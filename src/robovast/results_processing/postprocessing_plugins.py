# Copyright (C) 2025 Frederik Pasch
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

"""Default postprocessing command plugins for RoboVAST.

This module provides built-in postprocessing commands that can be referenced
by name in the configuration file.

All plugins must inherit from :class:`BasePostprocessingPlugin`.  Each plugin
must implement :meth:`~BasePostprocessingPlugin.__call__` to execute the
postprocessing logic. Optionally override :meth:`~BasePostprocessingPlugin.get_files_to_copy`
to declare additional files (e.g. helper scripts) that must be copied into the
``_config/`` directory so that they are available at execution time.

Each plugin's ``__call__`` method accepts a results_dir parameter containing the
path to the results directory (parent of campaign-* dirs) or campaign-<id> directory to
process, along with a config_dir for resolving relative paths, and additional
command-specific parameters.

Each ``__call__`` method returns a tuple of (success: bool, message: str).

Configuration format:
    postprocessing:
      - plugin_name:
          param1: value1
          param2: value2
      - simple_plugin_name
"""
import csv
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import tarfile
from importlib.resources import files
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from robovast.common import log_summary, scenario_markers
from robovast.common.execution import (COMPAT_VERSION, MIN_IMAGE_COMPAT,
                                       is_campaign_dir)
from robovast.results_processing.csv_types import (INTEGER, REAL, TEXT, UNKNOWN, cast_expr,
                                                   column_def, infer_column_types, sql_value, widen,
                                                   widest)

logger = logging.getLogger(__name__)


def _retype_table(conn, table: str, final_types: dict) -> list[str]:
    """Rebuild *table* so its declared column types match *final_types*.

    A column's type is declared by the first run that writes it, but the evidence is
    every run: a later run can turn an ``INTEGER`` column real, or a numeric column
    textual. Leaving the declaration behind is not cosmetic — a schema that claims
    ``REAL`` over a column holding one ``'n/a'`` makes ``AVG()`` return a plausible
    wrong number (SQLite reads the text as 0) and ``MAX()`` return the text itself.
    So once every run has been seen, tables whose verdict moved are rebuilt with the
    right types and their values brought over (see
    :func:`~robovast.results_processing.csv_types.cast_expr`).

    Returns the columns whose declared type changed (empty when nothing was rebuilt).
    """
    info = [(r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{table}")')]
    # PRAGMA reports a column declared without a type as an empty string.
    declared = {name: (typ or UNKNOWN) for name, typ in info}
    changed = [name for name, typ in declared.items()
               if final_types.get(name, typ) != typ]
    if not changed:
        return []

    types = {name: final_types.get(name, declared[name]) for name, _ in info}
    col_defs = ", ".join(column_def(n, types[n]) for n, _ in info)
    col_list = ", ".join(f'"{n}"' for n, _ in info)
    select = ", ".join(cast_expr(n, types[n]) for n, _ in info)
    staging = f"_retype_{table}"
    # Rename-then-copy rather than ALTER (SQLite cannot change a column's type), and
    # drop the staging table before recreating the index so the index name is free.
    conn.execute(f'ALTER TABLE "{table}" RENAME TO "{staging}"')
    conn.execute(f'CREATE TABLE "{table}" ({col_defs})')
    conn.execute(f'INSERT INTO "{table}" ({col_list}) SELECT {select} FROM "{staging}"')
    conn.execute(f'DROP TABLE "{staging}"')
    conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_ctx" '
                 f'ON "{table}" (config_name, run_id)')
    return changed


class BasePostprocessingPlugin:
    """Base class for class-based postprocessing plugins.

    Subclasses must implement :meth:`__call__` with the standard plugin
    signature.  Override :meth:`get_files_to_copy` to declare additional files
    that should be copied into the campaign ``_config/`` directory before
    execution (e.g. helper scripts referenced by the plugin).
    """

    #: Whether this plugin must run in the **campaign's own execution image** rather than
    #: in whatever process is orchestrating it.
    #:
    #: Declared by the plugin instead of listed in the caller, so a new one that needs the
    #: image -- another deserializer, a tool only the SUT image carries -- is dispatched
    #: correctly without anyone editing the orchestrator. A name list there would silently
    #: serve only the plugins that existed when it was written.
    #:
    #: The concrete case: deserializing a rosbag needs the ROS 2 message definitions the
    #: runs recorded with, which exist only in that image. Running such a plugin in the
    #: orchestrating process instead resolves an image against the wrong project and exits
    #: non-zero, and everything downstream then reads files that were never written.
    needs_execution_image: bool = False

    def __call__(
        self,
        results_dir: str,
        config_dir: str,
        **kwargs,
    ) -> Tuple[bool, str]:
        """Execute the postprocessing plugin.

        Args:
            results_dir: Path to the campaign-<id> directory to process.
            config_dir: Directory containing the .vast config file (used to
                resolve relative paths).
            **kwargs: Plugin-specific keyword arguments from the config.

        Returns:
            Tuple of (success, message).
        """
        raise NotImplementedError("Subclasses must implement __call__.")

    def get_files_to_copy(self, config_dir: str, params: dict) -> List[str]:
        """Return file paths (relative to *config_dir*) that must be copied.

        Override this method to declare additional files that the plugin needs
        at execution time.  The returned paths are relative to *config_dir* and
        will be copied into the campaign ``_config/`` directory so that they
        are available as ``_config/<path>`` inside the execution container.

        Args:
            config_dir: Absolute path to the directory containing the .vast
                config file.
            params: The plugin parameters dict from the .vast config, i.e. the
                same keyword arguments that will be passed to :meth:`__call__`.

        Returns:
            List of relative file paths (relative to *config_dir*) to copy.
        """
        return []


class Command(BasePostprocessingPlugin):
    """Execute an arbitrary command or script.

    Generic plugin that allows execution of any command or script path.
    Use this for custom scripts or when a specific plugin doesn't exist.

    The script (when given as a relative path) is automatically copied into
    the campaign ``_config/`` directory so that it is available to the
    execution container without manual setup.

    Example usage in .vast config:

    .. code-block:: yaml

        postprocessing:
          - command:
              script: postprocess.sh
          - command:
              script: ../../../tools/docker_exec.sh
              args: [custom_script.py, --arg, value]
          - command:
              script: /absolute/path/to/script.sh
    """

    def get_files_to_copy(self, config_dir: str, params: dict) -> List[str]:
        """Return the script path if it is a relative path that exists.

        Args:
            config_dir: Directory containing the .vast config file.
            params: Plugin parameters, expected to contain ``script``.

        Returns:
            List with the relative script path when it resolves to an
            existing file; empty list otherwise.
        """
        script = params.get('script')
        if not script or os.path.isabs(script):
            return []
        candidate = os.path.join(config_dir, script)
        if os.path.isfile(candidate):
            return [script]
        return []

    def __call__(
        self,
        results_dir: str,
        config_dir: str,
        script: str,
        args: Optional[List[str]] = None,
        provenance_file: Optional[str] = None,
        execution_image: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Execute the configured script.

        Args:
            results_dir: Path to the campaign-<id> directory to process
            config_dir: Directory containing the config file (for resolving relative paths)
            script: Script path to execute (relative or absolute)
            args: Optional list of command-line arguments to pass to the script
            provenance_file: Optional path for provenance JSON (passed to script if it supports it)
            execution_image: Ignored by this plugin (accepted for interface compatibility)

        Returns:
            Tuple of (success, message)
        """
        # Resolve script path if not absolute
        script_path = script
        if not os.path.isabs(script_path) and config_dir:
            script_path = os.path.join(config_dir, script_path)

        if not os.path.exists(script_path):
            return False, f"Script not found: {script_path}"

        # Build full command (optionally pass provenance to docker_exec and script)
        full_command = [script_path]
        if provenance_file:
            full_command.extend(["--provenance-file", provenance_file])
        if args:
            full_command.extend(args)
        full_command.append(results_dir)

        try:
            result = subprocess.run(
                full_command,
                cwd=results_dir,
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'}
            )

            if result.returncode != 0:
                return False, f"Command failed with exit code {result.returncode}\n{result.stderr}"

            output = result.stdout.strip()
            return True, f"Command executed successfully\n{output}" if output else "Command executed successfully"

        except Exception as e:
            return False, f"Error executing command: {e}"


def _interrupted_job_dirs(results_dir: str) -> list:
    """Campaign-relative artifact dirs of the jobs that were cut short rather than run out.

    **The shared seam for this pipeline's missing-data rule.** A step that cannot read
    something a stopped job should have produced *describes the gap and succeeds* — it does
    not fail the campaign. That rule is not new here: :func:`run_slices._read_window`
    states it ("a run killed mid-flight never wrote ``test.xml``. That is not an error
    here"), and ``run_log`` / ``resource_usage`` implement it by reporting through
    :func:`~robovast.results_processing.run_slices.describe_missing`. Only a genuine
    conversion error fails a step.

    ``rosbags_process`` was the one step that did not follow it — it exits non-zero on any
    unreadable bag — so one stopped job failed the whole campaign's postprocessing and cost
    the metrics of every job that did finish. Any future step that *scans* rather than
    iterating the run table should consult this rather than inventing a second answer.

    Two kinds qualify, for one reason. ``killed``: an operator stopped the job by hand.
    ``invalid``: the runner threw the trial away because a container it depended on crashed
    and was restarted under it. Both end with ``delete_namespaced_job`` at
    ``grace_period_seconds=0``, i.e. a recorder SIGKILLed mid-write and a bag that can never
    be opened — so treating them differently here would reintroduce exactly the failure the
    rule above exists to prevent, one layer down: one invalidated trial costing the metrics
    of every job in the campaign that did finish.

    ``[]`` for every campaign nobody intervened in — the ledger file does not exist — so
    this costs one missing-file check on the normal path and changes nothing about it.

    Read here rather than passed in because the postprocessing pipeline runs where the
    campaign is (in-cluster, against the object-store mount), and its plugins take their
    inputs from ``results_dir``; there is no caller in that process holding the kill.
    """
    from robovast.common.campaign_data import (KIND_INVALID, KIND_KILLED,
                                                read_interventions)
    try:
        entries = read_interventions(results_dir)
    except Exception:  # noqa: BLE001 - never let the ledger break postprocessing
        return []
    return sorted({e["job_dir"] for e in entries
                   if e.get("job_dir") and e.get("kind") in (KIND_KILLED, KIND_INVALID)})


class RosbagsProcess(BasePostprocessingPlugin):
    # Reads rosbags, so it needs the image whose message definitions wrote them.
    needs_execution_image = True

    """Unified single-pass rosbag processor with internal plugin system.

    Reads each rosbag exactly once and dispatches messages to all configured
    handler plugins. This is significantly more efficient than running separate
    ``rosbags_*`` scripts when multiple data types are needed from the same bags.

    This class is used automatically by the postprocessing orchestrator, which
    batches all ``rosbags_*`` commands from the ``.vast`` config into a single
    call. It can also be used directly in ``.vast`` configs.

    Available handler types: ``to_csv``, ``tf_to_csv``, ``nav2_bt_to_csv``,
    ``action_to_csv``, ``rosout_to_csv``, ``costmap_to_csv``, ``to_webm``.

    Example direct usage in .vast config:

    .. code-block:: yaml

        postprocessing:
          - rosbags_process:
              plugins:
                - type: tf_to_csv
                  frames: [base_link]
                - type: to_csv
                  topics: [/cmd_vel, /odom]
                - type: rosout_to_csv
    """

    def __call__(
        self,
        results_dir: str,
        config_dir: str,
        plugins: List[dict],
        workers: Optional[int] = None,
        bag_dir: Optional[str] = None,
        provenance_file: Optional[str] = None,
        execution_image: Optional[str] = None,
        debug: bool = False,
        force: bool = False,
    ) -> Tuple[bool, str]:
        """Execute rosbags_process plugin.

        Args:
            results_dir: Path to the campaign-<id> directory to process.
            config_dir: Directory containing the config file.
            plugins: List of handler config dicts, each with a ``type`` key.
            workers: Optional number of parallel workers.
            bag_dir: Rosbag subdirectory name to search for (default: "rosbag2").
            provenance_file: Optional path for provenance JSON.
            execution_image: Optional Docker image override.
            debug: If True, print all per-bag output; otherwise show only progress/summary.

        Returns:
            Tuple of (success, message).
        """
        if not plugins:
            return False, "rosbags_process requires at least one entry under 'plugins'"

        script_path = str(files('robovast.results_processing.data').joinpath('docker_exec.sh'))
        config_json = json.dumps({"plugins": plugins})

        cmd = [script_path, "--compat-version", str(COMPAT_VERSION),
               "--min-compat-version", str(MIN_IMAGE_COMPAT)]
        if execution_image:
            cmd.extend(["--image", execution_image])
        if provenance_file:
            cmd.extend(["--provenance-file", provenance_file])
        cmd.append("rosbags_process.py")
        if provenance_file:
            cmd.extend(["--provenance-file", f"/provenance/{os.path.basename(provenance_file)}"])
        cmd.extend(["--config", config_json])
        if workers is not None:
            cmd.extend(["--workers", str(workers)])
        if bag_dir is not None:
            cmd.extend(["--bag-dir", bag_dir])
        # A job stopped by an operator, or invalidated by the runner after a container
        # crashed under it, was SIGKILLed mid-write — so its rosbag is unfinalized and
        # cannot be opened, ever. Without this the campaign's whole postprocessing step
        # fails on that one bag, which would mean one interrupted job costs the analysis of
        # every job that DID finish.
        for job_dir in _interrupted_job_dirs(results_dir):
            cmd.extend(["--tolerate-under", job_dir])
        if debug:
            cmd.append("--debug")
        if force:
            cmd.append("--force")
        cmd.append(results_dir)

        try:
            # Stream output line-by-line so progress is visible in real-time.
            process = subprocess.Popen(
                cmd,
                cwd=os.path.dirname(script_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr into stdout to avoid deadlock
                text=True,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'},
            )
            output_lines: List[str] = []
            _last_was_progress = False
            for line in process.stdout:
                line = line.rstrip("\n")
                output_lines.append(line)
                is_progress = line.startswith("Processing rosbags")
                if is_progress and not debug:
                    print(f"\r{line}", end="", flush=True)
                else:
                    if _last_was_progress and not debug:
                        print()
                    print(line, flush=True)
                _last_was_progress = is_progress
            if _last_was_progress and not debug:
                print()
            returncode = process.wait()
            output = "\n".join(output_lines)
            if returncode != 0:
                return False, f"rosbags_process failed with exit code {returncode}\n{output}"
            summary = next(
                (line for line in output_lines if line.startswith("Summary:")),
                "rosbags processed successfully",
            )
            return True, summary
        except Exception as e:
            return False, f"Error executing rosbags_process: {e}"


class RunLog(BasePostprocessingPlugin):
    """Merge every container's log with ``/rosout`` into one ``run_log.csv`` per run.

    Auto-injected for every campaign (see
    :data:`~robovast.results_processing.postprocessing.AUTO_PLUGINS`), because a run whose
    output cannot be read afterwards cannot be explained — the same argument ``bt_log``
    makes for itself. Declare it explicitly only to change a parameter, or list it under
    ``skip`` to opt out.

    What it does, and why each part is not optional, is documented in
    :mod:`robovast.results_processing.run_log`. In outline: the logs are written **per
    job** (``_jobs/<batch>/job-N/logs/``), so each run resolves its job through the
    campaign's ``job_links.yaml`` manifest — not the ``job`` symlink, which only appears
    once a job has finished and cannot exist in an object store at all. The output lands in
    the **run** directory, where ``generate_data_db``'s glob already looks, so the
    ``run_log`` table needs no ingest code of its own.

    Example usage in .vast config (only needed to override a default):

    .. code-block:: yaml

       postprocessing:
         - run_log:
             min_severity: warn
    """

    def __call__(
        self,
        results_dir: str,
        config_dir: str = "",  # pylint: disable=unused-argument
        min_severity: str = "",
        **kwargs,  # pylint: disable=unused-argument
    ) -> Tuple[bool, str]:
        """Write ``<config>/<run>/run_log.csv`` for every run of the campaign.

        Args:
            results_dir: The campaign directory to process.
            config_dir: Unused; the plugin reads only campaign output.
            min_severity: Keep only rows this severe (``warn``/``error``). Empty keeps
                everything, which is the default: a filter applied at *write* time cannot
                be undone without re-running postprocessing, and the reading surfaces all
                have their own severity control.
            **kwargs: The runner injects lane-dependent extras — ``provenance_file``,
                ``execution_image``, ``debug``, ``force`` — and only those it has
                (``execution_image`` is set on the cluster and not locally). Naming a subset
                therefore passes on one lane and raises ``unexpected keyword argument`` on
                the other, which is exactly how this plugin first failed on the cluster after
                passing locally. This plugin needs none of them: it reads the campaign's own
                output and runs in-process.

        Returns:
            Tuple of (success, message).
        """
        from . import run_log, run_slices  # pylint: disable=import-outside-toplevel

        campaign_path = Path(results_dir)
        if not campaign_path.is_dir():
            return False, f"Campaign directory does not exist: {results_dir}"

        sole_container = _sole_container(campaign_path)

        floor = None
        if min_severity:
            try:
                floor = log_summary.severity_rank(min_severity)
            except ValueError as e:
                return False, str(e)

        # One job serves many runs in a packed campaign, and reading its logs is the
        # expensive part, so parse each job once and slice it per run.
        job_cache: Dict[str, list] = {}
        totals = run_log.MergeStats()
        slices = run_slices.SliceStats()
        runs_written = 0

        # Grouped by job, not streamed one run at a time: the boundary between two runs of a
        # packed job is a property of the job's whole run set AND of its log (see below), so
        # every run of a job has to be in hand before any of them can be cut.
        by_job: Dict[str, List] = {}
        for slice_ in run_slices.iter_run_slices(campaign_path, slices):
            by_job.setdefault(slice_.job_dir, []).append(slice_)

        for job_dir, job_slices in by_job.items():
            if job_dir not in job_cache:
                stats = run_log.MergeStats()
                job_cache[job_dir] = run_log.collect_job_records(
                    job_dir, stats, sole_container=sole_container)
                totals.add_job(stats)
            records = job_cache[job_dir]

            # A log takes the run's LOG claim -- a partition, like a measurement takes, but
            # cut at the other end of the gap between two runs (see run_slices). A packed job
            # runs several *different configurations* in sequence, and another configuration's
            # trial is a different experiment, not context for this one: giving every run all
            # of the job's lines gave every run the FIRST scenario's verdict, so a run whose
            # own trial passed read as failed.
            #
            # The boundaries come from the scenario-start lines when they can be matched to
            # the runs, because ``test.xml``'s start is recorded microseconds AFTER the line
            # is logged -- close enough to file every run's own marker with its predecessor.
            # This is the one place that can do it: the partition needs the log, and only this
            # plugin reads it.
            #
            # ``in_window`` still separates the trial from this run's own bring-up, verdict
            # and teardown *inside* its claim -- the part a sample cannot express.
            markers = [r.wall_ts for r in records if r.wall_ts is not None
                       and scenario_markers.is_scenario_start(r.message)
                       and scenario_markers.is_own_logger(r.node)]
            snapped = run_slices.log_claims_from_markers(
                [(s.job_name, s.start_epoch) for s in job_slices], markers)

            for slice_ in job_slices:
                start, end = (snapped[slice_.job_name] if snapped
                              else (slice_.log_claim_start, slice_.log_claim_end))
                rows = run_log.rows_for_window(
                    [r for r in records
                     if run_slices.claims_log(r.wall_ts, start, end)],
                    slice_.clock, start_epoch=slice_.start_epoch,
                    end_epoch=slice_.end_epoch)
                if floor is not None:
                    rows = [r for r in rows
                            if log_summary.severity_rank(r["severity"]) >= floor]
                run_log.write_run_log(str(slice_.run_dir / run_log.FILENAME), rows)
                totals.rows += len(rows)
                runs_written += 1

        if not runs_written:
            return True, "run_log: no runs found"
        message = f"run_log: {runs_written} run(s), {totals.summary()}"
        message += run_slices.describe_missing("no job artifacts for", slices.without_job)
        if slices.without_clock:
            # Loud, because every row of these runs has an empty sim_time and the reading
            # surfaces will say "not aligned" without explaining why.
            message += run_slices.describe_missing(
                "no clock map for", slices.without_clock) + " -- wall time only"
        return True, message


class ResourceUsage(BasePostprocessingPlugin):
    """Slice each job's resource-monitor samples into one ``resource_usage.csv`` per run.

    Auto-injected for every campaign (see
    :data:`~robovast.results_processing.postprocessing.AUTO_PLUGINS`), because what a run
    cost is a competing explanation for what it did: a lane gives a job a fixed number of
    cores, and a simulator that starves the stack changes the stack's behaviour. That can
    only be ruled out in the same query as the behaviour, which means a table.

    What it does, and why each part is not optional, is documented in
    :mod:`robovast.results_processing.resource_usage`. In outline: every container writes
    ``resource_usage_<container>.csv`` per **job** (``_jobs/<batch>/job-N/``), so each run
    resolves its job through the campaign's ``job_links.yaml`` manifest — not the ``job``
    symlink, which only appears once a job has finished and cannot exist in an object store
    at all. The output lands in the **run** directory, where ``generate_data_db``'s glob
    already looks, so the ``resource_usage`` table needs no ingest code of its own.

    Unlike ``run_log``, a job's samples are **partitioned** between the runs it served
    rather than given to all of them: another run's CPU is not this run's, and copying it
    would make every aggregate over a packed campaign report a multiple of the truth.

    Takes no parameters. Every filter one might add (a container, a severity, a decimation)
    is a ``WHERE`` clause the reader already has, and one applied at write time cannot be
    undone without re-running postprocessing.
    """

    def __call__(
        self,
        results_dir: str,
        config_dir: str = "",  # pylint: disable=unused-argument
        **kwargs,  # pylint: disable=unused-argument
    ) -> Tuple[bool, str, list]:
        """Write ``<config>/<run>/resource_usage.csv`` for every run of the campaign.

        Args:
            results_dir: The campaign directory to process.
            config_dir: Unused; the plugin reads only campaign output.
            **kwargs: The runner injects lane-dependent extras — ``provenance_file``,
                ``execution_image``, ``debug``, ``force`` — and only those it has
                (``execution_image`` is set on the cluster and not locally). Naming a subset
                therefore passes on one lane and raises ``unexpected keyword argument`` on
                the other, which is how ``RunLog`` first failed on the cluster after passing
                locally. This plugin needs none of them.

        Returns:
            Tuple of (success, message, provenance entries).
        """
        from robovast.common.campaign_data import \
            campaign_container_plan  # pylint: disable=import-outside-toplevel

        from . import resource_usage, run_slices  # pylint: disable=import-outside-toplevel

        campaign_path = Path(results_dir)
        if not campaign_path.is_dir():
            return False, f"Campaign directory does not exist: {results_dir}", []

        # From the container PLAN, never the .vast's container keys: a `simulation` block
        # with neither image nor command is folded into the scenario container, so a campaign
        # with two keys can have run one -- and expecting a file per key would report a
        # container that never existed as having gone missing.
        plan = campaign_container_plan(campaign_path)
        expected = resource_usage.expected_container_files(plan)

        job_cache: Dict[str, tuple] = {}
        totals = resource_usage.ScanStats()
        slices = run_slices.SliceStats()
        entries: List[dict] = []
        runs_written = 0
        root = campaign_path.parent

        for slice_ in run_slices.iter_run_slices(campaign_path, slices):
            if slice_.job_dir not in job_cache:
                stats = resource_usage.ScanStats()
                label = os.path.relpath(slice_.job_dir, campaign_path)
                ticks = resource_usage.collect_job_ticks(
                    slice_.job_dir, expected, stats, label)
                sources = sorted(
                    os.path.relpath(os.path.join(slice_.job_dir, name), root)
                    for name in os.listdir(slice_.job_dir)
                    if name.startswith("resource_usage_") and name.endswith(".csv")
                ) if os.path.isdir(slice_.job_dir) else []
                job_cache[slice_.job_dir] = (ticks, sources)
                totals.add_job(stats)
            ticks, sources = job_cache[slice_.job_dir]

            rows = resource_usage.rows_for_slice(ticks, slice_)
            output = slice_.run_dir / resource_usage.FILENAME
            run_slices.write_csv(str(output), resource_usage.FIELDNAMES, rows)
            totals.rows += len(rows)
            runs_written += 1
            entries.append({
                "output": os.path.relpath(output, root),
                "sources": sources,
                "plugin": "resource_usage",
                "params": {},
            })

        if not runs_written:
            return True, "resource_usage: no runs found", []
        if not totals.rows:
            # Lead with it: the status tooltip shows the first line only, and "no data" is
            # not something a reader should have to notice by the absence of a table.
            message = f"resource_usage: NO resource data ({runs_written} run(s))"
        else:
            message = f"resource_usage: {runs_written} run(s), {totals.summary()}"
        if expected is None:
            message += "; expected container set unknown -- took the files found"
        message += run_slices.describe_missing("no job artifacts for", slices.without_job)
        # A container the plan named that recorded nothing is a finding, not an absence:
        # most often a vanilla sidecar image without psutil, where the monitor dies before
        # it opens the file and the entrypoint never checks.
        message += run_slices.describe_missing("no CSV for", totals.missing, "container(s)")
        message += run_slices.describe_missing("empty CSV for", totals.empty, "container(s)")
        message += run_slices.describe_missing(
            "truncated CSV for", totals.truncated, "container(s)")
        message += run_slices.describe_missing(
            "unreadable CSV for", totals.unreadable, "container(s)")
        message += run_slices.describe_missing(
            "unexpected CSV for", totals.unexpected, "container(s)")
        message += run_slices.describe_missing(
            "no clock map for", slices.without_clock) + (
                " -- wall time only" if slices.without_clock else "")
        message += run_slices.describe_missing(
            "no test.xml, so no samples claimed, for", slices.unplaceable)
        return True, message, entries


class Compress(BasePostprocessingPlugin):
    """Create a gzipped tarball for each campaign-* directory (runs on host).

    For each direct subdirectory of results_dir whose name starts with ``campaign-``,
    creates a ``<campaign-name>-<id>.tar.gz`` in the output directory containing that campaign's
    contents. Does not use Docker; runs entirely on the host using Python's
    tarfile module. Useful for archiving or transferring results.

    output_dir must not be inside the results directory (would break postprocessing
    hash caching). Relative paths are resolved from the directory containing the
    .vast file (config_dir).

    Example usage in .vast config:

    .. code-block:: yaml

       postprocessing:
         - compress:
             output_dir: archives
         - compress:
             output_dir: /path/to/archives
             overwrite: false
    """

    def __call__(
        self,
        results_dir: str,
        config_dir: str,
        output_dir: Optional[str] = None,
        exclude_dirs: Optional[List[str]] = None,
        overwrite: bool = True,
        provenance_file: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Execute compress plugin.

        Args:
            results_dir: Path to the results directory (parent of campaign* dirs).
            config_dir: Directory containing the .vast config file; relative output_dir
                is resolved from here.
            output_dir: Where to write tarballs. If not set, defaults to config_dir.
                Relative paths are resolved from config_dir. Must not be inside
                results_dir.
            exclude_dirs: Directory names to exclude from the tarball (default: ['.cache']).
                Pass an empty list to include everything.
            overwrite: If True (default), recreate and overwrite existing tarballs.
                If False, skip run dirs that already have a corresponding .tar.gz in the
                output directory.
            provenance_file: Optional path for provenance JSON

        Returns:
            Tuple of (success, message).
        """
        # Resolve output_dir from config_dir (relative to .vast file dir); default = config_dir
        if output_dir:
            out_dir = os.path.normpath(
                os.path.join(config_dir, output_dir) if not os.path.isabs(output_dir) else output_dir
            )
        else:
            out_dir = os.path.abspath(config_dir)
        out_abs = Path(out_dir).resolve()
        results_abs = Path(results_dir).resolve()
        # Forbid writing into results directory so postprocessing hashing is not affected
        if out_abs == results_abs or (out_abs != results_abs and results_abs in out_abs.parents):
            return False, (
                f"compress output_dir must not be inside the results directory "
                f"(would break postprocessing hash). output_dir={out_dir!r}, results_dir={results_dir!r}. "
                f"Use a path outside results (e.g. relative to .vast dir: output_dir: archives)."
            )
        exclude = set(exclude_dirs if exclude_dirs is not None else [".cache"])

        root = Path(results_dir)
        if not root.is_dir():
            return False, f"Results directory does not exist: {results_dir}"

        created = []
        for campaign_item in sorted(root.iterdir()):
            if not campaign_item.is_dir() or not is_campaign_dir(campaign_item.name):
                continue
            if campaign_item.name == "_config":
                continue

            tarball_path = Path(out_dir) / f"{campaign_item.name}.tar.gz"
            if not overwrite and tarball_path.exists():
                continue
            try:
                os.makedirs(out_dir, exist_ok=True)
                with tarfile.open(tarball_path, "w:gz") as tf:
                    for entry in campaign_item.rglob("*"):
                        if not entry.is_file():
                            continue
                        if any(part in exclude for part in entry.relative_to(campaign_item).parts):
                            continue
                        tf.add(entry, arcname=campaign_item.name + "/" + str(entry.relative_to(campaign_item)))
                created.append(tarball_path.name)
            except OSError as e:
                return False, f"Failed to create {tarball_path}: {e}"

        if not created:
            return True, "No campaign* directories found or all tarballs already exist (use overwrite: true to recreate)"
        return True, f"Created tarballs: {', '.join(created)}"


# Reserved campaign-level directory names (not config dirs)
# NOTE: the campaign layout (which dirs are reserved vs. configurations) is defined
# once in robovast.common.campaign_data — use list_config_dirs()/list_run_dirs()
# rather than re-deriving it here (a local copy drifted and counted `_jobs` as a
# configuration).


#: Per-column warnings a caller needs *before* aggregating, keyed by ``(table, column)``.
#: They live in ``_column_notes`` rather than in the table's description because that is
#: where ``describe_campaign_data`` shows them — beside the column, which is where someone
#: about to write ``AVG(...)`` is looking — and because a description is prose spent on
#: every request while a note is only carried by the table that has one.
#:
#: Written only for columns that actually exist in this campaign's data.db, so a note never
#: describes a table that was never produced.
#: Notes for a table that follows the POSE CONTRACT (see ``docs/results_processing.rst``). Keyed on
#: the column, and attached to any table carrying a ``stamp`` column rather than to a list of table
#: names -- the contract is what a table *has*, not what it is called, so a new producer's table is
#: annotated without registering it here.
#:
#: These three are exactly where an agent writing SQL against a pose table goes wrong.
_POSE_CONTRACT_NOTES = {
    "timestamp": (
        "ARRIVAL time, and the join key every other table in this campaign shares -- use it to "
        "read poses against costmaps, behaviors and run_log, and to place a row on the run "
        "view's timeline. Do NOT difference it: it is quantized to the simulator's /clock grid "
        "and jittered by delivery, so a speed derived from it measures the transport rather than "
        "the robot. Use `stamp` for that."),
    "stamp": (
        "MEASUREMENT time -- when the pose was actually true, from the publisher's own header. "
        "This is the correct base for any derivative (speed, rate, dt); sort by it too, since "
        "ordering by `timestamp` leaves rows within one arrival tick in arbitrary order. NULL "
        "where the producer could not state one (a latched /tf_static transform)."),
    "orientation.yaw": (
        "DERIVED at ingest from orientation.x/y/z/w, and a planar projection: correct for a body "
        "in the plane, insufficient for one that pitches or rolls (a drone, a tilting arm, a "
        "robot on a ramp). The quaternion is what the producer emitted -- read that when the "
        "body is not flat."),
}

_STATIC_COLUMN_NOTES: dict[tuple[str, str], str] = {
    ("resource_usage", "cpu_percent"): (
        "one row is one PROCESS NAME, not a container: SUM per (container, wall_ts) before "
        "comparing, or an average reads as a per-process figure. Per-core, so >100 is "
        "normal — full saturation is 100 * runs.available_cpus, which is the denominator to "
        "normalise by before comparing runs on different hosts."),
    ("resource_usage", "memory_rss_bytes"): (
        "summed RSS, so pages shared between a process and its forks are counted more than "
        "once. An upper bound — read it as a trend, not as an absolute footprint."),
    ("runs", "probed"): (
        "1 when a human read into this run WHILE IT RAN, which is a fact about the run's "
        "provenance and not about its outcome — a probed run can still pass, so this is a "
        "separate column and never folded into status. Exclude these rows from anything a "
        "published number rests on. Granularity follows the job: with runs_per_job > 1 the "
        "whole packed job is marked, since which of its runs was in flight cannot be "
        "recovered — it over-excludes rather than admitting a perturbed run."),
}


def _csv_to_table_name(filename: str) -> str:
    """Convert a data filename to a valid SQLite table name.

    Strips the .csv/.jsonl extension, replaces non-alphanumeric/underscore characters
    with underscores, lowercases, and prefixes with 't_' if it starts with a digit.

    Examples:
        ``behaviors.csv``              -> ``behaviors``
        ``behaviors.jsonl``            -> ``behaviors``
        ``resource_usage_cpu.csv``     -> ``resource_usage_cpu``
        ``action-nav.csv``             -> ``action_nav``
        ``1_metric.csv``               -> ``t_1_metric``
    """
    stem = filename
    for suffix in (".csv", ".jsonl"):
        if stem.lower().endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", stem).lower()
    if sanitized and sanitized[0].isdigit():
        sanitized = "t_" + sanitized
    return sanitized or "t_unknown"


#: py_trees' status names -> the numeric codes the ``behaviors`` table has always
#: carried (they come from ``py_trees_ros_interfaces/Behaviour``, not from py_trees,
#: whose ``Status`` values are strings). Kept so ``behaviors`` and ``nav2_behaviors``
#: stay one schema.
_BT_STATUS_CODES = {"INVALID": 1, "RUNNING": 2, "SUCCESS": 3, "FAILURE": 4}


def _read_behaviour_tree_log(records: list) -> list:
    """Rows for the ``behaviors`` table from a ``behaviour_tree_log`` JSONL file.

    Written by scenario_execution's ``--bt-log``, which records the behaviour tree
    without ROS in the loop. The first record is metadata and is dropped here; the
    rest are already one-row-per-status-change, so this only adds the numeric
    ``status`` alongside the name, keeping the seven columns the table has always had
    (and that ``nav2_behaviors`` mirrors) plus the fields the JSONL adds.
    """
    rows = []
    for record in records[1:]:
        row = dict(record)
        status_name = row.pop("status", None)
        row["status"] = _BT_STATUS_CODES.get(status_name)
        row["status_name"] = status_name
        rows.append(row)
    return rows


#: JSONL ``format`` -> the function turning its records into table rows. Dispatching
#: on the file's own declared format rather than its name keeps the ingest open to
#: further producers without hardcoding filenames here.
#:
#: BOTH SPELLINGS, and both are load-bearing. scenario-execution renamed the format it
#: writes from ``behaviour_tree_log`` to ``behavior_tree_log`` (matching the spelling of
#: its own records), so which one arrives depends on the image a run used -- and runs
#: already archived carry the old one forever. Dropping either spelling does not fail: an
#: unknown format makes ``_read_table_rows`` return no rows, so the ``behaviors`` table is
#: simply empty and the run view's scenario tree is blank with nothing saying why.
_JSONL_READERS = {"behaviour_tree_log": _read_behaviour_tree_log,
                  "behavior_tree_log": _read_behaviour_tree_log}


_QUAT_COLUMNS = ("orientation.x", "orientation.y", "orientation.z", "orientation.w")
_YAW_COLUMN = "orientation.yaw"


def _derive_yaw(rows: list) -> None:
    """Add ``orientation.yaw`` to pose rows that carry a quaternion and no yaw, in place.

    Producers emit a quaternion and only a quaternion: Euler angles are lossy the moment a body
    pitches or rolls, so a drone or a tilting arm cannot be described by them, and nothing should
    have to convert before it can report a pose. But the 2D consumers here -- the costmap panel's
    heading marker, the nav MCP tools, the notebooks -- all want a heading, and none of them should
    each reimplement quaternion math in SQL, JavaScript and pandas.

    So it is derived once, here, and marked in ``_column_notes`` as the projection it is: correct
    for a body in the plane, insufficient for one that has left it.

    Sniffed rather than configured -- any table with the quaternion columns and no yaw gets one --
    so a new producer satisfying the pose contract is served without registering anything.
    """
    if not rows or _YAW_COLUMN in rows[0] or any(c not in rows[0] for c in _QUAT_COLUMNS):
        return
    for row in rows:
        x, y, z, w = (_as_float(row.get(c)) for c in _QUAT_COLUMNS)
        if None in (x, y, z, w):
            row[_YAW_COLUMN] = ""
            continue
        row[_YAW_COLUMN] = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def _read_table_rows(path: Path) -> list:
    """Rows for one data file, or ``[]`` if it is unreadable or not a known format."""
    if path.suffix.lower() == ".jsonl":
        try:
            with open(path, encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]
        except Exception:  # pylint: disable=broad-except
            return []
        if not records or not isinstance(records[0], dict):
            return []
        reader = _JSONL_READERS.get(records[0].get("format"))
        return reader(records) if reader else []
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:  # pylint: disable=broad-except
        return []
    _derive_yaw(rows)
    return rows


def _as_float(value) -> Optional[float]:
    """A CSV cell as a float, or ``None`` when it is empty or not a number.

    A ``run_log`` timestamp is legitimately empty -- the clock map does not extrapolate,
    so a line it cannot place has no ``sim_time`` -- and that is a fact to record, not an
    error to raise.
    """
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sole_container(campaign_path) -> Optional[str]:
    """The one container this campaign runs, or ``None`` when it runs more than one.

    Read from the campaign's own ``.vast`` snapshot rather than from the log files a job happens
    to have produced: a sidecar may be a vanilla image that never runs RoboVAST's entrypoint and
    so writes no log, and inferring "one container" from that would mislabel its rows. Returns
    the *runtime* container name (:data:`~robovast.common.log_tail.MAIN_CONTAINER`), not the
    ``scenario`` role name the ``.vast`` uses.

    Counted from the container PLAN, not from the snapshot's ``execution.containers`` keys: a
    ``simulation`` block with neither image nor command is folded into the scenario container,
    so such a campaign has two keys and runs one container. Counting keys made it look like
    two, and every unattributable line of a single-container campaign then kept an empty
    ``container`` instead of the only one it could have come from.
    """
    from robovast.common.campaign_data import \
        campaign_container_plan  # pylint: disable=import-outside-toplevel
    from robovast.common.log_tail import MAIN_CONTAINER  # pylint: disable=import-outside-toplevel
    plan = campaign_container_plan(Path(campaign_path))
    if plan is None:
        return None
    return MAIN_CONTAINER if len(plan.names()) == 1 else None


def _clock_map_info(campaign_path, config_name: str, run_id: int):
    """What relates this run's wall-stamped log to sim time, and how well.

    Read here rather than passed in because ``runs`` is the one table a reader joins to when
    asking "can I trust this run's ``sim_time``?" — the answer belongs beside the run's other
    facts, not in a second place to look. A run whose map is missing reports
    :data:`~robovast.results_processing.clock_map.SOURCE_NONE`, which is a finding: its log
    is wall-time only.
    """
    from robovast.common.execution import \
        job_artifact_dir  # pylint: disable=import-outside-toplevel

    from . import clock_map  # pylint: disable=import-outside-toplevel
    run_dir = Path(campaign_path) / config_name / str(run_id)
    try:
        job_dir = job_artifact_dir(str(campaign_path), f"{config_name}/{run_id}")
    except (FileNotFoundError, OSError):
        job_dir = None
    if job_dir:
        found = clock_map.load_clock_map(
            os.path.join(job_dir, "logs", clock_map.FILENAME))
        if found:
            return found.info
    # Non-ROS: roqsim streams its own map beside the run's recording.
    return clock_map.find_run_clock_map(str(run_dir)).info


def _build_runs_table(conn, campaign_path, config_dirs) -> None:
    """Create a ``runs`` dimension table: per-run status/duration + scenario params.

    Params (and the objective) come from ``campaign.db``'s ``unit`` table, keyed
    by ``config_name``; per-run status/duration come from ``campaign.db``'s ``run``
    table (the operational source of truth, written live from each ``test.xml``) —
    this ``runs`` table is the analytics-wide *view* over it, joining sysinfo and
    exploding params into ``param_`` columns. Joining ``runs`` to any metric table
    on ``(config_name, run_id)`` answers questions like "how does <param> affect
    <metric>" in one query. A store predating the ``run`` table falls back to
    re-parsing ``test.xml`` directly.
    """
    from datetime import datetime, timedelta  # pylint: disable=import-outside-toplevel

    from robovast.common.campaign_data import \
        read_run_outcome  # pylint: disable=import-outside-toplevel
    from robovast.common.quantity import to_bytes  # pylint: disable=import-outside-toplevel

    def _end_time(start_iso, duration_sec):
        """ISO end time = start + duration, or None when either is unavailable."""
        if not start_iso or duration_sec is None:
            return None
        try:
            return (datetime.fromisoformat(start_iso)
                    + timedelta(seconds=float(duration_sec))).isoformat()
        except (ValueError, TypeError):
            return None

    def _sysinfo_fields(outcome):
        """Host info for a run (instance_type/cpu_name/cpus/mem) from its outcome dict.

        Prefers ``campaign.db``'s ``job.sysinfo_json`` (``sysinfo_json``) over re-walking
        each run's ``sysinfo.yaml``: the store already resolved that file once at
        execution time, across its three historical locations, so this cannot disagree
        with ``campaign.job`` the way a second independent read could. Also accepts the
        ``sysinfo`` dict that :func:`read_run_outcome` attaches on the fallback path,
        where a store too old to have a ``job`` table forces a read from disk.
        """
        si = outcome.get("sysinfo")
        if si is None:
            raw = outcome.get("sysinfo_json")
            if not raw:
                return None, None, None, None
            try:
                si = json.loads(raw) or {}
            except (TypeError, ValueError):
                return None, None, None, None
        # ``available_mem`` is the key sysinfo actually writes, and its value is a
        # Kubernetes-style quantity — a plain byte count from /proc/meminfo or the
        # downward API, or a suffixed string like "16Gi" when the .vast set a limit. It is
        # normalized to bytes here so the column can be compared and averaged; reading it
        # raw would make the column numeric in some runs and text in others.
        return (si.get("instance_type"), si.get("cpu_name"),
                si.get("available_cpus"), to_bytes(si.get("available_mem")))

    # Resolved params + objective per config, and per-run outcomes, from
    # campaign.db (read-only). ``outcomes[config_name][run_id]`` holds the stored
    # status/passed/errors/failures/duration/start_time; empty when the store
    # predates the ``run`` table, in which case the loop re-parses ``test.xml``.
    params_by_config: dict[str, dict] = {}
    objective_by_config: dict[str, object] = {}
    outcomes: dict[str, dict[int, dict]] = {}
    # Search draws that never composed: they have no config_name and no directory on
    # disk, so the config-dir walk below cannot see them. Carried separately (keyed by
    # paramset_id, the only identity they have) and appended as run-less rows, because
    # a campaign that could not build half of what it proposed must not read as one
    # that proposed less.
    composition_failed: list[tuple[str, dict]] = []
    campaign_db = campaign_path / "campaign.db"
    if campaign_db.exists():
        cc = sqlite3.connect(f"file:{campaign_db}?mode=ro", uri=True)
        try:
            # status/paramset_id are absent from a store predating them; fall back to
            # the columns every version has rather than losing every unit's params.
            try:
                rows = cc.execute(
                    "SELECT config_name, params_json, objective, status, paramset_id "
                    "FROM unit").fetchall()
            except sqlite3.Error:
                rows = [(cn, pj, obj, None, None) for cn, pj, obj in cc.execute(
                    "SELECT config_name, params_json, objective FROM unit")]
            for cn, pj, obj, status, psid in rows:
                try:
                    params = json.loads(pj) if pj else {}
                except (TypeError, ValueError):
                    params = {}
                if status == "composition_failed":
                    composition_failed.append((cn or str(psid), params))
                    continue
                if not cn:
                    continue
                params_by_config[cn] = params
                objective_by_config[cn] = obj
        except sqlite3.Error:
            pass
        try:
            # LEFT JOIN job: the host record is per *job*, shared by the runs of a packed
            # multi-config job, and absent for a store written before the job table.
            for cn, rid, status, passed, errors, failures, duration, start, sysinfo in \
                    cc.execute(
                        "SELECT u.config_name, r.run_id, r.status, r.passed, r.errors, "
                        "r.failures, r.duration_s, r.start_time, j.sysinfo_json "
                        "FROM run r JOIN unit u ON r.unit_id = u.id "
                        "LEFT JOIN job j ON r.job_id = j.id"):
                outcomes.setdefault(cn, {})[rid] = {
                    "status": status, "passed": passed, "errors": errors,
                    "failures": failures, "duration_s": duration, "start_time": start,
                    "sysinfo_json": sysinfo}
        except sqlite3.Error:
            pass  # v1 store: no ``run``/``job`` table — fall back to test.xml below
        cc.close()

    # Resolved once: the ledger is absent for every campaign nobody touched, and then this is
    # an empty dict and no manifest is read at all.
    from robovast.common.campaign_data import probed_runs
    probed = probed_runs(campaign_path)

    base_cols = ["config_name", "run_id", "status", "passed",
                 "duration_s", "errors", "failures", "objective",
                 "start_time", "end_time",
                 "instance_type", "cpu_name", "available_cpus", "available_mem_bytes",
                 # What this run's log timestamps are worth. A reader that finds sim_time
                 # NULL in run_log needs to know whether the run was not aligned or simply
                 # logged nothing before the clock started, and "which source said so" is
                 # the difference. See results_processing.clock_map.
                 "clock_map_source", "clock_map_samples", "clock_map_wall_span_s",
                 # Whether a human read into this run while it was still going. A SEPARATE
                 # column and never folded into ``status``: a probed run can still pass, and
                 # putting an intervention into the measured outcome is the same mistake
                 # keeping ``killed`` out of ``num_failed`` avoids. Analysis that must not
                 # rest on a perturbed cell filters on this.
                 "probed"]
    param_keys = sorted({k for p in (*params_by_config.values(),
                                     *(p for _, p in composition_failed))
                         for k in p if f"param_{k}" not in base_cols})
    param_cols = [f"param_{k}" for k in param_keys]
    all_cols = base_cols + param_cols

    # Param columns are typed from the resolved param values across all configs, so
    # a numeric factor stays numeric: `ORDER BY param_wind_strength` and
    # `WHERE param_speed > 0.5` mean what they say instead of comparing text.
    # ``available_cpus`` is REAL, not INTEGER: ``execution.resources.cpu`` takes fractional
    # cores, and an INTEGER column would silently truncate a 0.5-core reservation to 0 — which
    # then reads as "this run had no CPU" in every query that joins to it.
    types = {c: (INTEGER if c in ("run_id", "passed", "errors", "failures",
                                  "available_mem_bytes", "clock_map_samples", "probed")
                 else REAL if c in ("duration_s", "objective", "clock_map_wall_span_s",
                                    "available_cpus")
                 else TEXT)
             for c in base_cols}
    for key, col in zip(param_keys, param_cols):
        types[col] = UNKNOWN
        for params in (*params_by_config.values(),
                       *(p for _, p in composition_failed)):
            if key in params:
                value = params[key]
                types[col] = widen(
                    types[col],
                    json.dumps(value) if isinstance(value, (list, dict)) else value)

    conn.execute("DROP TABLE IF EXISTS runs")
    col_defs = ", ".join(column_def(c, types[c]) for c in all_cols)
    conn.execute(f"CREATE TABLE runs ({col_defs})")
    conn.execute('CREATE INDEX idx_runs_ctx ON runs (config_name, run_id)')

    placeholders = ", ".join("?" for _ in all_cols)
    col_list = ", ".join(f'"{c}"' for c in all_cols)
    insert_sql = f"INSERT INTO runs ({col_list}) VALUES ({placeholders})"

    for config_dir in config_dirs:
        config_name = config_dir.name
        params = params_by_config.get(config_name, {})
        objective = objective_by_config.get(config_name)
        for run_dir in sorted(
                (d for d in config_dir.iterdir() if d.is_dir() and d.name.isdigit()),
                key=lambda d: int(d.name)):
            run_id = int(run_dir.name)
            # Stored outcome from campaign.db.run; else parse the run's test.xml (passing
            # the campaign root so that fallback still resolves the run's sysinfo.yaml).
            o = (outcomes.get(config_name, {}).get(run_id)
                 or read_run_outcome(run_dir, campaign_path))
            status = o["status"]
            passed = o["passed"]
            errors = o["errors"]
            failures = o["failures"]
            duration = o["duration_s"]
            start_time = o["start_time"]
            end_time = _end_time(start_time, duration)
            instance_type, cpu_name, avail_cpus, avail_mem = _sysinfo_fields(o)
            clock_info = _clock_map_info(campaign_path, config_name, run_id)
            base_vals = [config_name, run_id, status, passed, duration,
                         errors, failures, objective,
                         start_time, end_time,
                         instance_type, cpu_name, avail_cpus, avail_mem,
                         clock_info.source, clock_info.samples, clock_info.wall_span_s,
                         1 if f"{config_name}/{run_id}" in probed else 0]
            param_vals = [params.get(k) for k in param_keys]
            conn.execute(insert_sql, [sql_value(v, types[c])
                                      for c, v in zip(all_cols, base_vals + param_vals)])

    # The draws that never became a configuration. One row each, run_id NULL (there is
    # no run to number) and every run-derived column NULL — the parameters are the whole
    # point: they are what the search proposed and what turned out to be unrealizable.
    for identity, params in composition_failed:
        base_vals = [identity, None, "composition_failed", 0, None,
                     None, None, None,
                     None, None,
                     None, None, None, None,
                     None, None, None, 0]
        param_vals = [params.get(k) for k in param_keys]
        conn.execute(insert_sql, [sql_value(v, types[c])
                                  for c, v in zip(all_cols, base_vals + param_vals)])


def _build_postprocessing_steps_table(conn, campaign_path, name_map: dict) -> None:
    """Create ``postprocessing_steps``: how each table in this ``data.db`` was produced.

    ``data.db`` holds one table per CSV stem but says nothing about their derivation,
    while ``_transient/postprocessing.yaml`` records exactly that (``plugin`` / ``output``
    / ``sources`` / ``params`` per step) in a form no query can reach. This projects those
    entries into SQL so the provenance edge is joinable to the data:

        SELECT plugin, params_json FROM postprocessing_steps WHERE table_name = 'poses'

    ``table_name`` is resolved here rather than left to the caller, because this is where
    both halves are known — the entry's output path and *name_map*, the display-name to
    SQL-name mapping. A caller would have to re-derive a stem-to-table match and guess the
    sanitisation. It is NULL for a step whose output is not a CSV that became a table
    (a plot, a video, a merged artifact), which is a fact about the step, not a failure.

    The YAML remains the source of truth and is not replaced: it is what the FAIR/PROV-O
    export reads.
    """
    conn.execute(
        "CREATE TABLE postprocessing_steps ("
        "step_idx INTEGER, "
        "plugin TEXT, "
        "output TEXT, "
        "table_name TEXT, "
        "sources_json TEXT, "
        "params_json TEXT"
        ")"
    )
    path = campaign_path / "_transient" / "postprocessing.yaml"
    if not path.is_file():
        # No postprocessing ran, or it produced nothing — an empty table is the honest
        # answer, and still tells a caller the provenance question has a home.
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = data.get("entries") or []
    except (OSError, yaml.YAMLError) as e:
        logger.warning("Could not read %s for postprocessing provenance: %s", path, e)
        return
    for idx, ent in enumerate(entries):
        if not isinstance(ent, dict):
            continue
        output = ent.get("output") or ""
        conn.execute(
            "INSERT INTO postprocessing_steps "
            "(step_idx, plugin, output, table_name, sources_json, params_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (idx, ent.get("plugin") or None, output or None,
             name_map.get(Path(output).stem) if output else None,
             json.dumps(ent.get("sources") or [], default=str),
             json.dumps(ent.get("params") or {}, default=str)),
        )
    conn.commit()


def generate_data_db(campaign_dir: str, output_callback=None) -> tuple[bool, str]:
    """Consolidate all per-run data files into a single SQLite database.

    Creates ``<campaign_dir>/_execution/data.db`` (replacing any existing file).
    Each data filename (e.g. ``behaviors.csv``) becomes a separate table containing
    data from all configs and all runs, with extra ``config_name`` and ``run_id``
    columns prepended.

    Both ``*.csv`` and ``*.jsonl`` are picked up. A JSONL file declares its layout in
    the ``format`` key of its first record and is expanded to rows by the matching
    entry in :data:`_JSONL_READERS`; one a run produces today is ``behaviors.jsonl``,
    written directly by scenario_execution's ``--bt-log`` and therefore present for
    non-ROS runs too.

    Column types are inferred from the CSV values themselves
    (:mod:`robovast.results_processing.csv_types`): a column whose every non-empty
    value is a plain decimal number becomes ``INTEGER``/``REAL`` and is stored as a
    number, everything else stays ``TEXT``. Without that, every column is text and
    ``ORDER BY timestamp`` sorts ``"10.022"`` before ``"9.5"`` — a silent, plausible
    wrong answer rather than an error. A column that is numeric in one run and text
    in another is logged as a warning; both values are kept.

    A ``scenario_timestamps`` table is also created, holding one row per run: when its
    scenario reached a verdict and how, from the first such entry in the merged log
    (see :mod:`robovast.common.scenario_markers`). It carries the moment on both clocks
    — ``timestamp`` in sim seconds and ``wall_ts`` — because every surface that stops at
    the end of the trial reads it from here rather than matching the log text again.

    A ``_table_name_map`` table records the mapping from display names (CSV stems)
    to actual SQL table names.

    Args:
        campaign_dir: Path to a ``campaign-<id>`` directory.

    Returns:
        Tuple of (success, message).
    """
    from robovast.common.campaign_data import (  # pylint: disable=import-outside-toplevel
        list_config_dirs, list_run_dirs)

    def _log(msg: str) -> None:
        if output_callback:
            output_callback(msg)
        else:
            print(msg)

    # The layout constant lives with the reader that has to interpret it, and is imported here
    # rather than at module scope to keep pandas off the postprocessing import path.
    from robovast.common.analysis.db import DATA_DB_SCHEMA_VERSION

    campaign_path = Path(campaign_dir)
    if not campaign_path.is_dir():
        return False, f"Campaign directory does not exist: {campaign_dir}"

    exec_dir = campaign_path / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    db_path = exec_dir / "data.db"

    # Remove existing DB for clean rebuild
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        # Stamp the layout so a reader can say "this database predates the column you asked
        # for" instead of "no such column". No migrations go with it -- see
        # DATA_DB_SCHEMA_VERSION: this file is rebuilt from the run directories, so the
        # upgrade path for an old one is to run postprocessing again, which re-executes no
        # trial. PRAGMA cannot be parameterized; the version is a constant we control.
        conn.execute(f"PRAGMA user_version = {DATA_DB_SCHEMA_VERSION}")

        # Metadata table: display_name -> sql_table_name
        conn.execute(
            "CREATE TABLE _table_name_map "
            "(display_name TEXT PRIMARY KEY, sql_name TEXT NOT NULL)"
        )
        # Scenario timestamps. Two clocks, because they answer different questions:
        # ``timestamp`` is sim time and is what the playback timeline is measured in;
        # ``wall_ts`` is what the merged log is *ordered* by, and is the only one that
        # exists at all for a run whose /clock stopped before shutdown -- which is
        # exactly the run whose shutdown noise there is most of.
        conn.execute(
            "CREATE TABLE scenario_timestamps ("
            "config_name TEXT NOT NULL, "
            "run_id INTEGER NOT NULL, "
            "timestamp REAL, "
            "wall_ts REAL, "
            "status TEXT, "
            "message TEXT, "
            "PRIMARY KEY (config_name, run_id)"
            ")"
        )
        # Caveats a column's declared type cannot express, surfaced by
        # ``describe_data_db`` so they reach whoever writes the SQL (the postprocessing
        # log does not). Currently: a column that is numeric in some runs and text in
        # others.
        conn.execute(
            "CREATE TABLE _column_notes ("
            "table_name TEXT NOT NULL, "
            "column_name TEXT NOT NULL, "
            "note TEXT NOT NULL, "
            "PRIMARY KEY (table_name, column_name)"
            ")"
        )
        conn.commit()

        # Track which SQL tables have been created and their current columns
        # sql_table_name -> set of column names already in the schema
        created_tables: dict[str, set[str]] = {}
        # sql_table_name -> {column: SQLite type}, inferred from the CSV values
        # themselves (see robovast.results_processing.csv_types) so numeric columns
        # are stored numerically instead of as text that only sorts lexicographically.
        col_types: dict[str, dict[str, str]] = {}
        # (display_name, sql_table_name, column) whose values disagree across runs —
        # numeric in the run that created the column, text in a later one. Such a
        # column ends up TEXT, and both the log and `_column_notes` say so: an
        # aggregate over it silently reads the text rows as 0, so a caller has to know
        # to exclude them rather than trust AVG().
        mixed_columns: set[tuple[str, str, str]] = set()
        # display_name -> sql_table_name
        name_map: dict[str, str] = {}
        # display_name -> total row count across all runs
        table_rows: dict[str, int] = {}

        config_dirs = list_config_dirs(campaign_path)

        # Count total runs upfront for progress reporting
        all_run_dirs: list = []
        for config_dir in config_dirs:
            for d in list_run_dirs(config_dir):
                all_run_dirs.append((config_dir.name, d))
        total_runs = len(all_run_dirs)
        _log(f"  Building data.db from {total_runs} run(s) across {len(config_dirs)} config(s)...")

        _commit_batch = 500  # commit every N runs to reduce fsync overhead
        completed_runs = 0

        for config_dir in config_dirs:
            config_name = config_dir.name
            run_dirs = sorted(
                (d for d in config_dir.iterdir() if d.is_dir() and d.name.isdigit()),
                key=lambda d: int(d.name),
            )
            for run_dir in run_dirs:
                run_id = int(run_dir.name)
                scenario_ts: float | None = None
                scenario_wall_ts: float | None = None
                scenario_status: str | None = None
                scenario_msg: str | None = None
                # Track stems seen within this run to detect duplicate table names
                run_stem_to_path: dict[str, Path] = {}

                # JSONL sits alongside CSV: a run's behaviour tree log is written
                # directly by scenario_execution (no rosbag, so it also exists for
                # non-ROS runs) and lands in the same one-file-one-table scheme.
                data_paths = sorted(list(run_dir.rglob("*.csv")) + list(run_dir.rglob("*.jsonl")))
                for csv_path in data_paths:
                    display_name = csv_path.stem
                    sql_name = _csv_to_table_name(csv_path.name)

                    # Raise an error if two data files in the same run would map to the same table
                    if display_name in run_stem_to_path:
                        raise ValueError(
                            f"Duplicate table name '{display_name}' in run {run_id} of config "
                            f"'{config_name}': '{csv_path.relative_to(run_dir)}' conflicts with "
                            f"'{run_stem_to_path[display_name].relative_to(run_dir)}'"
                        )
                    run_stem_to_path[display_name] = csv_path

                    if display_name not in name_map:
                        name_map[display_name] = sql_name
                        conn.execute(
                            "INSERT OR IGNORE INTO _table_name_map (display_name, sql_name) VALUES (?, ?)",
                            (display_name, sql_name),
                        )

                    rows = _read_table_rows(csv_path)

                    if not rows:
                        continue

                    # Union rather than the first row's keys: a CSV is uniform, but a
                    # JSONL file need not be (a pruned-subtree record carries only
                    # `removed`), and a column seen late must still get one.
                    csv_cols = list(dict.fromkeys(
                        c for row in rows for c in row if isinstance(c, str)))

                    # When the scenario ended, and how. Recorded here and read everywhere
                    # else -- the playback clock, the log views, ``search_run_logs`` -- so
                    # that "when did the trial end" has one answer rather than a text match
                    # repeated per surface. The recognition itself lives in
                    # :mod:`robovast.common.scenario_markers`.
                    #
                    # ``timestamp`` is *sim* time, because that is what the column means to
                    # every reader: the run view unions this table into the playback range
                    # (``RunView.tsx``'s TIME_TABLES), so a wall-epoch value here stretches
                    # the timeline to 1.8e9 seconds and the progress bar becomes unusable.
                    # It is read from ``run_log``'s ``sim_time`` (already converted through
                    # the run's clock map) and from nothing else: rosout's own ``timestamp``
                    # is wall, and converting it here would duplicate the merge's work.
                    #
                    # ``wall_ts`` is the same event on the other clock, and is not redundant:
                    # the clock map does not extrapolate, so a run whose /clock stopped at
                    # shutdown has NULL ``sim_time`` on every line after it -- including,
                    # sometimes, the verdict itself. Ordering the log is a wall question.
                    #
                    # Gated on ``scenario_status`` rather than on ``scenario_ts``: a verdict
                    # the clock map cannot place has a status but no sim time, and gating on
                    # the timestamp would look for it again on the next file.
                    if csv_path.stem == "run_log" and scenario_status is None:
                        for row in rows:
                            msg_val = str(row.get("message", ""))
                            status = scenario_markers.verdict_of(
                                msg_val, str(row.get("node", "")))
                            if not status:
                                continue
                            scenario_ts = _as_float(row.get("sim_time"))
                            scenario_wall_ts = _as_float(row.get("wall_ts"))
                            scenario_status = status
                            scenario_msg = msg_val
                            break

                    context_cols = ["config_name", "run_id"]
                    all_data_cols = context_cols + csv_cols
                    csv_types = infer_column_types(rows, csv_cols)

                    if sql_name not in created_tables:
                        types = {"config_name": TEXT, "run_id": INTEGER, **csv_types}
                        col_types[sql_name] = types
                        col_defs = ", ".join(
                            column_def(c, types[c]) for c in all_data_cols
                        )
                        conn.execute(f'CREATE TABLE "{sql_name}" ({col_defs})')
                        conn.execute(
                            f'CREATE INDEX IF NOT EXISTS "idx_{sql_name}_ctx" '
                            f'ON "{sql_name}" (config_name, run_id)'
                        )
                        created_tables[sql_name] = set(all_data_cols)
                        conn.commit()
                    else:
                        # Add any new columns from this CSV
                        existing = created_tables[sql_name]
                        types = col_types[sql_name]
                        altered = False
                        for col in csv_cols:
                            if col not in existing:
                                types[col] = csv_types[col]
                                conn.execute(
                                    f'ALTER TABLE "{sql_name}" ADD COLUMN '
                                    f'{column_def(col, types[col])}')
                                existing.add(col)
                                altered = True
                                continue
                            # The declared type came from the first run that wrote this
                            # column, so a later run can disagree. Widening the stored
                            # type keeps values honest: SQLite's numeric affinity leaves
                            # a real in an INTEGER column as a real, and a column that
                            # was empty until now has no affinity to fight.
                            widened = widest(types[col], csv_types[col])
                            if widened != types[col]:
                                if widened == TEXT and types[col] != UNKNOWN:
                                    mixed_columns.add((display_name, sql_name, col))
                                types[col] = widened
                        if altered:
                            conn.commit()

                    placeholders = ", ".join("?" for _ in all_data_cols)
                    col_list = ", ".join(f'"{c}"' for c in all_data_cols)
                    insert_sql = f'INSERT INTO "{sql_name}" ({col_list}) VALUES ({placeholders})'
                    batch = [
                        [config_name, run_id] + [
                            sql_value(row.get(c), types[c]) for c in csv_cols
                        ]
                        for row in rows
                    ]
                    conn.executemany(insert_sql, batch)
                    table_rows[display_name] = table_rows.get(display_name, 0) + len(rows)

                # Record scenario timestamp (even if None)
                conn.execute(
                    "INSERT OR REPLACE INTO scenario_timestamps "
                    "(config_name, run_id, timestamp, wall_ts, status, message) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (config_name, run_id, scenario_ts, scenario_wall_ts,
                     scenario_status, scenario_msg),
                )

                completed_runs += 1
                if completed_runs % _commit_batch == 0:
                    conn.commit()
                    pct = completed_runs / total_runs * 100 if total_runs else 100
                    _log(f"  {completed_runs}/{total_runs} runs ({pct:.0f}%)")

        # Each column was declared from the first run that wrote it; the runs after it
        # may have widened the verdict. Now that every run has been seen, bring the
        # declarations back in line with the data — otherwise the schema an agent reads
        # contradicts what the table holds.
        retyped: dict[str, list[str]] = {}
        for sql_name, final_types in col_types.items():
            changed = _retype_table(conn, sql_name, final_types)
            if changed:
                retyped[sql_name] = changed
        for display_name, sql_name, col in sorted(mixed_columns):
            conn.execute(
                "INSERT OR REPLACE INTO _column_notes (table_name, column_name, note) "
                "VALUES (?, ?, ?)",
                (sql_name, col,
                 "numeric in some runs, text in others — stored as TEXT. An aggregate "
                 "reads the text rows as 0, so exclude them (e.g. WHERE col GLOB "
                 "'[0-9-]*') instead of trusting AVG/MIN/MAX."))
        for (table, col), note in _STATIC_COLUMN_NOTES.items():
            if table in created_tables and col in created_tables[table]:
                conn.execute(
                    "INSERT OR REPLACE INTO _column_notes (table_name, column_name, note) "
                    "VALUES (?, ?, ?)", (table, col, note))
        # What marks a table as following the pose contract: a measurement clock AND a position.
        # `stamp` alone is not enough -- rosout carries one too, and would collect notes that talk
        # about poses. Without `stamp`, the `timestamp` note would point at a column that is not
        # there.
        for table, columns in created_tables.items():
            if not {"stamp", "position.x"} <= set(columns):
                continue
            for col, note in _POSE_CONTRACT_NOTES.items():
                if col in columns:
                    conn.execute(
                        "INSERT OR REPLACE INTO _column_notes (table_name, column_name, note) "
                        "VALUES (?, ?, ?)", (table, col, note))
        conn.commit()

        # Dimension table joining per-run status/duration to scenario parameters,
        # so "how does <param> affect <metric>" is a single SQL join.
        _build_runs_table(conn, campaign_path, config_dirs)

        # How each of the tables above was produced — the provenance edge from a metric
        # back to the plugin and parameters that derived it.
        _build_postprocessing_steps_table(conn, campaign_path, name_map)

        # Final commit and persist name map
        conn.commit()
        table_count = len(created_tables)
    finally:
        conn.close()

    for display_name, row_count in sorted(table_rows.items()):
        _log(f"  table: {display_name} ({row_count} rows)")
    for sql_name, changed in sorted(retyped.items()):
        _log(f"  retyped {sql_name}: {', '.join(sorted(changed))} "
             f"(later runs widened the column)")
    for display_name, _sql_name, col in sorted(mixed_columns):
        # Not fatal — every value is still there — but the column is numeric in some
        # runs and text in others, so an aggregate over it will read the text rows as 0.
        # Also recorded in ``_column_notes``, which is where a SQL caller will see it.
        _log(f"  WARNING: {display_name}.{col} is numeric in some runs and text in "
             f"others; stored as TEXT. Exclude the text rows in aggregates, and check "
             f"the runs that write it.")

    return True, f"Created data.db with {table_count} table(s) in {db_path}"
