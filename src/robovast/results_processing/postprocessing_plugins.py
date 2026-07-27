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
import os
import re
import sqlite3
import subprocess
import tarfile
from importlib.resources import files
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from robovast.common.execution import COMPAT_VERSION, is_campaign_dir
from robovast.results_processing.csv_types import (INTEGER, REAL, TEXT, UNKNOWN,
                                                  cast_expr, column_def,
                                                  infer_column_types, sql_value, widen,
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


class RosbagsProcess(BasePostprocessingPlugin):
    """Unified single-pass rosbag processor with internal plugin system.

    Reads each rosbag exactly once and dispatches messages to all configured
    handler plugins. This is significantly more efficient than running separate
    ``rosbags_*`` scripts when multiple data types are needed from the same bags.

    This class is used automatically by the postprocessing orchestrator, which
    batches all ``rosbags_*`` commands from the ``.vast`` config into a single
    call. It can also be used directly in ``.vast`` configs.

    Available handler types: ``to_csv``, ``tf_to_csv``, ``bt_to_csv``,
    ``action_to_csv``, ``rosout_to_csv``.

    Example direct usage in .vast config:

    .. code-block:: yaml

        postprocessing:
          - rosbags_process:
              plugins:
                - type: tf_to_csv
                  frames: [base_link]
                - type: bt_to_csv
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

        cmd = [script_path, "--compat-version", str(COMPAT_VERSION)]
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


def _csv_to_table_name(filename: str) -> str:
    """Convert a CSV filename to a valid SQLite table name.

    Strips the .csv extension, replaces non-alphanumeric/underscore characters
    with underscores, lowercases, and prefixes with 't_' if it starts with a digit.

    Examples:
        ``behaviors.csv``              -> ``behaviors``
        ``resource_usage_cpu.csv``     -> ``resource_usage_cpu``
        ``action-nav.csv``             -> ``action_nav``
        ``1_metric.csv``               -> ``t_1_metric``
    """
    stem = filename
    if stem.lower().endswith(".csv"):
        stem = stem[:-4]
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", stem).lower()
    if sanitized and sanitized[0].isdigit():
        sanitized = "t_" + sanitized
    return sanitized or "t_unknown"


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
        return (si.get("instance_type"), si.get("cpu_name"),
                si.get("available_cpus"), si.get("available_mem_gb"))

    # Resolved params + objective per config, and per-run outcomes, from
    # campaign.db (read-only). ``outcomes[config_name][run_id]`` holds the stored
    # status/passed/errors/failures/duration/start_time; empty when the store
    # predates the ``run`` table, in which case the loop re-parses ``test.xml``.
    params_by_config: dict[str, dict] = {}
    objective_by_config: dict[str, object] = {}
    outcomes: dict[str, dict[int, dict]] = {}
    campaign_db = campaign_path / "campaign.db"
    if campaign_db.exists():
        cc = sqlite3.connect(f"file:{campaign_db}?mode=ro", uri=True)
        try:
            for cn, pj, obj in cc.execute(
                    "SELECT config_name, params_json, objective FROM unit"):
                if not cn:
                    continue
                try:
                    params_by_config[cn] = json.loads(pj) if pj else {}
                except (TypeError, ValueError):
                    params_by_config[cn] = {}
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

    base_cols = ["config_name", "run_id", "status", "passed",
                 "duration_s", "errors", "failures", "objective",
                 "start_time", "end_time",
                 "instance_type", "cpu_name", "available_cpus", "available_mem_gb"]
    param_keys = sorted({k for p in params_by_config.values() for k in p
                         if f"param_{k}" not in base_cols})
    param_cols = [f"param_{k}" for k in param_keys]
    all_cols = base_cols + param_cols

    # Param columns are typed from the resolved param values across all configs, so
    # a numeric factor stays numeric: `ORDER BY param_wind_strength` and
    # `WHERE param_speed > 0.5` mean what they say instead of comparing text.
    types = {c: (INTEGER if c in ("run_id", "passed", "errors", "failures",
                                  "available_cpus")
                 else REAL if c in ("duration_s", "objective", "available_mem_gb")
                 else TEXT)
             for c in base_cols}
    for key, col in zip(param_keys, param_cols):
        types[col] = UNKNOWN
        for params in params_by_config.values():
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
            base_vals = [config_name, run_id, status, passed, duration,
                         errors, failures, objective,
                         start_time, end_time,
                         instance_type, cpu_name, avail_cpus, avail_mem]
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
    """Consolidate all per-run CSV files into a single SQLite database.

    Creates ``<campaign_dir>/_execution/data.db`` (replacing any existing file).
    Each CSV filename (e.g. ``behaviors.csv``) becomes a separate table containing
    data from all configs and all runs, with extra ``config_name`` and ``run_id``
    columns prepended.

    Column types are inferred from the CSV values themselves
    (:mod:`robovast.results_processing.csv_types`): a column whose every non-empty
    value is a plain decimal number becomes ``INTEGER``/``REAL`` and is stored as a
    number, everything else stays ``TEXT``. Without that, every column is text and
    ``ORDER BY timestamp`` sorts ``"10.022"`` before ``"9.5"`` — a silent, plausible
    wrong answer rather than an error. A column that is numeric in one run and text
    in another is logged as a warning; both values are kept.

    A ``scenario_timestamps`` table is also created containing the timestamp of
    the first scenario-end rosout entry per run (from ``scenario_execution_ros``
    log messages).

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

        # Metadata table: display_name -> sql_table_name
        conn.execute(
            "CREATE TABLE _table_name_map "
            "(display_name TEXT PRIMARY KEY, sql_name TEXT NOT NULL)"
        )
        # Scenario timestamps
        conn.execute(
            "CREATE TABLE scenario_timestamps ("
            "config_name TEXT NOT NULL, "
            "run_id INTEGER NOT NULL, "
            "timestamp REAL, "
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
                scenario_status: str | None = None
                scenario_msg: str | None = None
                # Track stems seen within this run to detect duplicate table names
                run_stem_to_path: dict[str, Path] = {}

                for csv_path in sorted(run_dir.rglob("*.csv")):
                    display_name = csv_path.stem
                    sql_name = _csv_to_table_name(csv_path.name)

                    # Raise an error if two CSV files in the same run would map to the same table
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

                    try:
                        with open(csv_path, encoding="utf-8", newline="") as f:
                            reader = csv.DictReader(f)
                            rows = list(reader)
                    except Exception:
                        continue

                    if not rows:
                        continue

                    csv_cols = [c for c in rows[0].keys() if isinstance(c, str)]

                    # Extract scenario timestamp from rosout rows
                    if csv_path.stem == "rosout" and scenario_ts is None:
                        for row in rows:
                            name_val = str(row.get("name", ""))
                            msg_val = str(row.get("msg", ""))
                            if name_val == "scenario_execution_ros":
                                if msg_val.startswith("Scenario '") and msg_val.endswith("' succeeded."):
                                    try:
                                        ts_str = row.get("timestamp", "")
                                        scenario_ts = float(ts_str) if ts_str else None
                                    except (ValueError, TypeError):
                                        scenario_ts = None
                                    scenario_status = "succeeded"
                                    scenario_msg = msg_val
                                    break
                                if ": execution failed." in msg_val:
                                    try:
                                        ts_str = row.get("timestamp", "")
                                        scenario_ts = float(ts_str) if ts_str else None
                                    except (ValueError, TypeError):
                                        scenario_ts = None
                                    scenario_status = "failed"
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
                    "(config_name, run_id, timestamp, status, message) VALUES (?, ?, ?, ?, ?)",
                    (config_name, run_id, scenario_ts, scenario_status, scenario_msg),
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
