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

"""MCP plugin for driving campaigns: validate, start, monitor, and stop.

Unlike the read-only result plugins, these tools launch and kill real compute.
They are backed by :class:`~robovast.mcp_server.campaign_registry.CampaignRegistry`,
a crash-safe run-state registry that gives RoboVAST a single authoritative
"is a campaign running?" answer and enforces **local single-flight**: while a
local campaign is live, a second local start is refused (concurrent local runs
would collide on the shared Docker resources RoboVAST uses today).

.. warning::

   The MCP server has no authentication. These tools are registered
   unconditionally, so only expose the server on a trusted network (it binds
   ``127.0.0.1`` by default for this reason).
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP

from robovast.mcp_server.campaign_registry import CampaignRegistry, _proc_start_time

logger = logging.getLogger(__name__)

# Popen handles for local campaigns launched by *this* server process, so we can
# reap them (poll) — capturing the authoritative exit code and avoiding zombies
# that would otherwise still answer ``os.kill(pid, 0)``. Lost on server restart,
# in which case liveness falls back to /proc + the on-disk heuristic.
_LOCAL_PROCS: dict = {}
_PROCS_LOCK = threading.Lock()

_STOP_GRACE_SECONDS = 10


# -- Helpers -----------------------------------------------------------------


def _load_project():
    """Load the project config. Raises ValueError if the project is not initialized."""
    from robovast.common.cli.project_config import ProjectConfig
    project = ProjectConfig.load()
    if project is None or not project.config_path or not project.results_dir:
        raise ValueError(
            "Project not initialized. Run 'vast init <config-file>' first.")
    return project


def _service_client():
    """Return a ``RobovastClient`` bound to a configured robovast-service, or None.

    When ``ROBOVAST_SERVICE_URL`` is set, the control tools drive that service
    (a local ``vast serve``, a remote VM, or a cluster deployment) through the
    :class:`~robovast.service.interface.RobovastInterface` contract instead of
    spawning ``vast exec`` subprocesses — the client-server path in which the
    service is the execution authority and its own status tracking replaces the
    local subprocess + ``CampaignRegistry`` machinery. Unset (the default) keeps
    the in-process subprocess path below, so nothing changes for local/cluster
    users who have not opted into a service yet.
    """
    url = os.environ.get("ROBOVAST_SERVICE_URL", "")
    if not url:
        return None
    from robovast.service.client import RobovastClient
    return RobovastClient(url)


def _classify_dead_local_for(results_dir):
    """Return a ``entry -> status`` classifier for dead local entries.

    Authoritative when the child's exit code was captured; otherwise falls back
    to the on-disk signal (``campaign.db`` present + enough terminal runs).
    """
    def classify(entry):
        exit_code = entry.get("exit_code")
        if exit_code == 0:
            return "finished"
        if exit_code is not None:
            return "crashed"
        campaign_id = entry.get("campaign_id")
        campaign_dir = os.path.join(results_dir, campaign_id)
        from robovast.common.store import STORE_FILENAME
        if not os.path.exists(os.path.join(campaign_dir, STORE_FILENAME)):
            return "crashed"
        done, _passed, _failed = _read_progress(campaign_dir)
        expected = entry.get("expected_total")
        if expected and done >= expected:
            return "finished"
        return "crashed"
    return classify


def _registry(results_dir):
    """Build a registry bound to ``results_dir`` with the disk-based classifier."""
    return CampaignRegistry(
        results_dir, classify_dead_local=_classify_dead_local_for(results_dir))


def _reap_local_procs(registry):
    """Poll tracked child processes, recording exit codes for any that finished.

    Reaps zombies and writes the authoritative terminal status into the
    registry so single-flight and status reflect reality immediately.
    """
    with _PROCS_LOCK:
        finished = []
        for campaign_id, proc in list(_LOCAL_PROCS.items()):
            rc = proc.poll()
            if rc is not None:
                finished.append((campaign_id, rc))
                del _LOCAL_PROCS[campaign_id]
    for campaign_id, rc in finished:
        registry.update(
            campaign_id, exit_code=rc,
            status="finished" if rc == 0 else "crashed",
            finished_at=datetime.now().isoformat())


def _read_progress(campaign_dir):
    """Return ``(runs_done, passed, failed)`` from a campaign dir, tolerant of early state."""
    from robovast.common.campaign_data import get_vast_configuration_info
    try:
        info = get_vast_configuration_info(Path(campaign_dir))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return 0, 0, 0
    done = info.get("num_runs", 0)
    passed = info.get("num_passed", 0)
    failed = info.get("num_failed", 0) + info.get("num_errors", 0)
    return done, passed, failed


def _log_tail(log_path, max_lines=40):
    """Return the last ``max_lines`` lines of a log file, or ``""``."""
    if not log_path or not os.path.exists(log_path):
        return ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-max_lines:])
    except OSError:
        return ""


def _validate(project, config_filter):
    """Validate the project config and count configs/runs (same math as ``vast config info``).

    Returns ``(campaign_config, info_dict)`` used internally by ``start_campaign``
    (which needs the parsed ``campaign_config`` for the campaign id). Rich,
    collect-all validation for clients lives in the ``validate_project`` tool.
    """
    from robovast.common.common import load_config
    from robovast.common.config import validate_config
    from robovast.common.config_generation import generate_scenario_variations

    campaign_config = validate_config(load_config(project.config_path))
    campaign_data, _ = generate_scenario_variations(
        variation_file=project.config_path, output_dir=None)
    configs = campaign_data["configs"]
    if config_filter:
        import fnmatch
        configs = [c for c in configs
                   if fnmatch.fnmatch(c.get("name", ""), config_filter)]
    runs_per_config = campaign_data.get("execution", {}).get("runs", 1)
    info = {
        "valid": True,
        "configs": len(configs),
        "runs_per_config": runs_per_config,
        "total_trials": len(configs) * runs_per_config,
        "errors": [],
    }
    return campaign_config, info


# -- Tool functions ----------------------------------------------------------


def validate_project(config_path: str = "") -> dict:
    """Validate a RoboVAST project (``.vast`` file), reporting ALL problems at once.

    A ``.vast`` file defines a *project* (a campaign is one execution of it). This
    checks the whole file — YAML, schema, the scenario file, scenario-parameter
    references, and every plugin reference (variation types and their parameters,
    the ``results_processing``/``search`` postprocessing commands, and the search
    strategy/extractor), whether installed entry-point names or local
    ``./path.py:Class`` file refs — and returns **every** problem it finds in one
    pass, each tagged with the config block and field, so the file can be fixed in
    as few iterations as possible. When valid, it also returns the config/run
    counts (same math as ``vast config info``). Same collect-all core as the
    ``vast configuration validate`` CLI command.

    Args:
        config_path: Path to the ``.vast`` file. Empty uses the initialized
            project's config; pass an explicit path to validate any ``.vast``
            before a project has been initialized.

    Returns:
        ``{valid, configs, runs_per_config, total_trials, problems}`` where each
        problem is ``{stage, config, field, message}``.
    """
    from robovast.common.config_validation import validate_project_file
    try:
        path = config_path or _load_project().config_path
        return validate_project_file(path)
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"valid": False, "configs": 0, "runs_per_config": 0,
                "total_trials": 0,
                "problems": [{"stage": "project", "config": None,
                              "field": None, "message": str(e)}]}


def preview_configurations(config_path: str = "", max_configs: int = 0) -> dict:
    """Preview the resolved configurations a ``.vast`` would generate — WITHOUT running.

    ``validate_project`` returns only the counts; this returns the actual resolved
    per-configuration parameter sets, so you can eyeball what each variation cell
    expands to before starting a campaign (the read-only, in-memory equivalent of
    ``vast configuration generate`` / ``vast exec local prepare-run``, which stage
    the same tree to disk). Nothing is executed and nothing is written.

    Args:
        config_path: Path to the ``.vast`` file. Empty uses the initialized
            project's config.
        max_configs: Cap the number of configurations returned (``0`` = all). The
            ``configs`` count always reflects the true total; ``truncated`` marks
            when the returned list was shortened.

    Returns:
        ``{configs, runs_per_config, total_trials, configurations, truncated}``
        where each configuration is ``{name, parameters}`` and ``parameters`` is
        the resolved parameter-name → value mapping for that cell. On failure,
        ``{error}``.
    """
    from robovast.common.config_generation import generate_scenario_variations
    try:
        path = config_path or _load_project().config_path
        campaign_data, _ = generate_scenario_variations(
            variation_file=path, output_dir=None)
        configs = campaign_data["configs"]
        runs = campaign_data.get("execution", {}).get("runs", 1)
        items = [{"name": c["name"], "parameters": c.get("config", {})}
                 for c in configs]
        truncated = bool(max_configs) and len(items) > max_configs
        if truncated:
            items = items[:max_configs]
        return {
            "configs": len(configs),
            "runs_per_config": runs,
            "total_trials": len(configs) * runs,
            "configurations": items,
            "truncated": truncated,
        }
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"error": str(e)}


def init_project(config_path: str, project_dir: str, results_dir: str = "",
                 log_level: str = "INFO") -> dict:
    """Initialize a RoboVAST project (write ``.robovast_project``) from a ``.vast`` file.

    A ``.vast`` file defines a project; this creates the workspace that ties it to
    a results directory (the equivalent of ``vast init``, minus the interactive
    Docker/Kubernetes checks). The ``.vast`` is validated first — if it has any
    problems, **nothing is written** and every problem is returned so it can be
    fixed and retried.

    Because the server has no reliable working directory, all paths are explicit.
    Note that read/analysis tools discover a project by walking up from the
    server's working directory, so initialize where the server runs (or analyze a
    campaign folder directly by passing its absolute path).

    Args:
        config_path: Path to the ``.vast`` file (resolved to absolute).
        project_dir: Directory to write ``.robovast_project`` into.
        results_dir: Where campaign results go. Empty → ``<project_dir>/results``.
        log_level: Project log level (DEBUG/INFO/WARNING/ERROR/CRITICAL).

    Returns:
        ``{project_file, config_path, results_dir}`` on success; on an invalid
        ``.vast`` ``{error, problems: [...]}`` (no file written).
    """
    try:
        from robovast.common.cli.project_config import ProjectConfig
        from robovast.common.config_validation import validate_project_file

        abs_config = os.path.abspath(os.path.expanduser(config_path))
        report = validate_project_file(abs_config)
        if not report.get("valid"):
            return {"error": "invalid .vast", "config_path": abs_config,
                    "problems": report.get("problems", [])}

        abs_project_dir = os.path.abspath(os.path.expanduser(project_dir))
        os.makedirs(abs_project_dir, exist_ok=True)
        abs_results = (os.path.abspath(os.path.expanduser(results_dir))
                       if results_dir else os.path.join(abs_project_dir, "results"))

        project = ProjectConfig(config_path=abs_config, results_dir=abs_results,
                                log_level=log_level)
        project.save(target_dir=abs_project_dir)
        return {"project_file": os.path.join(abs_project_dir, ".robovast_project"),
                "config_path": abs_config, "results_dir": abs_results}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def start_campaign(config_filter: str = "", runs: int = 0,
                   backend: str = "local", context: str = "",
                   workspace_id: str = "", config_path: str = "") -> dict:
    """Start a campaign and return immediately; results land on local disk either way.

    Validates the campaign first and refuses if invalid. Both backends run as a
    detached child process whose log is written under
    ``_control/logs/<campaign_id>.log``:

    * ``local`` — ``vast exec local run`` (Docker on this host). Enforces
      single-flight: if a local campaign is already live, the launch is refused
      with the list of running campaigns.
    * ``cluster`` — ``vast exec cluster run --wait-and-download`` (Kubernetes):
      launches the in-cluster controller, waits for it to finish, and downloads
      the results into the project results directory — so a cluster campaign is
      as transparent as a local one. Cluster campaigns are not single-flighted.

    Args:
        config_filter: Optional glob to run only matching configurations.
        runs: Runs per configuration; ``0`` uses the value from the ``.vast`` file.
        backend: ``"local"`` (Docker on this host) or ``"cluster"`` (Kubernetes).
        context: Kubernetes context for ``cluster`` (empty = active context).
        workspace_id: When a service is configured, run this workspace's project
            (empty = the CWD project). Independent of the backend.
        config_path: Which ``.vast`` to run when the workspace has several
            (workspace-relative; empty = the sole ``.vast``).

    Returns:
        ``{campaign_id, backend, log_path}`` on success; ``{error, ...}`` on
        refusal or failure.
    """
    try:
        client = _service_client()
        if client is not None:
            # Client-server path: the service is the execution authority and
            # picks the backend by its own deployment (backend/context args are
            # ignored here — they are implicit in which service is configured).
            from robovast.service.interface import CreateCampaignRequest
            ref = client.create_campaign(CreateCampaignRequest(
                workspace_id=workspace_id, config_path=config_path,
                config_filter=config_filter,
                runs=runs if runs and runs > 0 else 1))
            return {"campaign_id": ref.campaign_id, "backend": "service"}

        project = _load_project()
        campaign_config, info = _validate(project, config_filter)
        if not info["valid"]:
            return {"error": "invalid campaign", "details": info["errors"]}
        if info["total_trials"] == 0:
            return {"error": "no configurations match", "config_filter": config_filter}
        if backend not in ("local", "cluster"):
            return {"error": f"unknown backend {backend!r}; use 'local' or 'cluster'"}

        from robovast.execution.controller import campaign_id_for
        campaign_id = campaign_id_for(campaign_config)
        runs_arg = runs if runs and runs > 0 else info["runs_per_config"]
        expected_total = info["configs"] * runs_arg

        results_dir = str(project.results_dir)
        os.makedirs(results_dir, exist_ok=True)
        registry = _registry(results_dir)
        registry.ensure_dirs()
        _reap_local_procs(registry)  # clear any just-finished entry before the guard
        log_path = registry.log_path_for(campaign_id)

        if backend == "local":
            ok, running = registry.reserve_local(
                campaign_id=campaign_id, config_filter=config_filter, runs=runs,
                expected_total=expected_total, log_path=log_path)
            if not ok:
                return {"error": "campaign already running", "running": running}
            cmd = [sys.executable, "-m", "robovast.common.cli.cli",
                   "exec", "local", "run", "--campaign-id", campaign_id,
                   "--output", results_dir, "--no-gui"]
        else:  # cluster
            registry.reserve_cluster(
                campaign_id=campaign_id, config_filter=config_filter, runs=runs,
                expected_total=expected_total, log_path=log_path, context=context)
            cmd = [sys.executable, "-m", "robovast.common.cli.cli",
                   "exec", "cluster", "run", "--wait-and-download",
                   "--campaign-id", campaign_id]
            if context:
                cmd += ["--context", context]
        if config_filter:
            cmd += ["--config", config_filter]
        if runs and runs > 0:
            cmd += ["--runs", str(runs)]

        return _spawn_tracked(registry, campaign_id, backend, cmd, results_dir, log_path)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _spawn_tracked(registry, campaign_id, backend, cmd, results_dir, log_path):
    """Spawn a detached child, redirect its log, and record its pid in the registry."""
    try:
        log_file = open(log_path, "ab")  # noqa: SIM115 - handed to the child, closed below
    except OSError as e:
        registry.update(campaign_id, status="failed", finished_at=datetime.now().isoformat())
        return {"error": f"cannot open log file: {e}"}
    try:
        proc = subprocess.Popen(  # noqa: S603 - args are constructed, not shell
            cmd, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=results_dir)
    except OSError as e:
        registry.update(campaign_id, status="failed", finished_at=datetime.now().isoformat())
        return {"error": f"failed to launch: {e}"}
    finally:
        log_file.close()

    with _PROCS_LOCK:
        _LOCAL_PROCS[campaign_id] = proc
    registry.attach_pid(campaign_id, proc.pid, _proc_start_time(proc.pid))
    logger.info("Started %s campaign %s (pid %s)", backend, campaign_id, proc.pid)
    return {"campaign_id": campaign_id, "backend": backend, "log_path": str(log_path)}


def get_campaign_status(campaign_id: str) -> dict:
    """Report the current status and progress of a campaign.

    Reconciles liveness first (a killed run is reported ``crashed``, a completed
    one ``finished``), then reads progress from the on-disk results. Both
    backends are handled uniformly: a cluster campaign's results appear locally
    once its ``--wait-and-download`` child finishes, and its live controller
    phase is visible in ``log_tail`` meanwhile.

    Args:
        campaign_id: The id returned by :func:`start_campaign`.

    Returns:
        ``{campaign_id, backend, status, runs_done, runs_total, passed, failed,
        log_tail}``; ``{error}`` if unknown.
    """
    try:
        client = _service_client()
        if client is not None:
            st = client.get_status(campaign_id)
            return {
                "campaign_id": campaign_id,
                "backend": "service",
                "status": st.phase,
                "runs_done": st.runs.completed,
                "runs_total": st.runs.total,
                "log_tail": st.stage or "",
            }

        project = _load_project()
        results_dir = str(project.results_dir)
        registry = _registry(results_dir)
        _reap_local_procs(registry)
        entry = registry.reconcile_and_get(campaign_id)
        if entry is None:
            return {"error": f"campaign {campaign_id!r} not tracked"}
        campaign_dir = os.path.join(results_dir, campaign_id)
        done, passed, failed = _read_progress(campaign_dir)
        return {
            "campaign_id": campaign_id,
            "backend": entry.get("backend"),
            "status": entry.get("status"),
            "runs_done": done,
            "runs_total": entry.get("expected_total"),
            "passed": passed,
            "failed": failed,
            "log_tail": _log_tail(entry.get("log_path")),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def stop_campaign(campaign_id: str) -> dict:
    """Stop a running campaign.

    Both backends terminate the launched process group (SIGTERM, then SIGKILL
    after a grace period). Local additionally reaps the campaign's Docker
    container; cluster additionally sends the controller a cooperative ``stop``
    (killing the local ``--wait-and-download`` waiter alone would not stop the
    in-cluster controller).

    Args:
        campaign_id: The id returned by :func:`start_campaign`.

    Returns:
        ``{campaign_id, stopped, status}`` or ``{error}``.
    """
    try:
        client = _service_client()
        if client is not None:
            res = client.stop(campaign_id)
            return {"campaign_id": campaign_id, "stopped": res.ok,
                    "status": "stopping", "note": res.message}

        project = _load_project()
        results_dir = str(project.results_dir)
        registry = _registry(results_dir)
        entry = registry.get(campaign_id)
        if entry is None:
            return {"error": f"campaign {campaign_id!r} not tracked"}
        backend = entry.get("backend")

        # Cluster: ask the in-cluster controller to stop cooperatively first, so
        # the campaign actually winds down rather than being orphaned when we
        # kill the local waiter below. Best-effort (may be unreachable).
        note = None
        if backend == "cluster":
            try:
                _cluster_cooperative_stop(campaign_id, entry)
                note = "cooperative stop requested; local waiter terminated"
            except Exception as e:  # noqa: BLE001
                note = f"could not reach controller ({e}); killed local waiter only"

        killed = _kill_tracked_process(campaign_id, entry)

        if backend == "local":
            # docker compose containers live in the daemon's tree and outlive the
            # process group; reap the hardcoded campaign container best-effort.
            with _suppress_process_errors():
                subprocess.run(["docker", "rm", "-f", "robovast"],  # noqa: S603,S607
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False, timeout=30)

        with _PROCS_LOCK:
            _LOCAL_PROCS.pop(campaign_id, None)
        registry.update(campaign_id, status="failed",
                        finished_at=datetime.now().isoformat())
        result = {"campaign_id": campaign_id, "stopped": killed, "status": "failed"}
        if note:
            result["note"] = note
        return result
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _kill_tracked_process(campaign_id, entry):
    """SIGTERM then SIGKILL the campaign's process group. Returns True if signalled."""
    pid = entry.get("pid")
    if not pid or entry.get("status") in ("finished", "failed", "crashed"):
        return False
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
        deadline = time.time() + _STOP_GRACE_SECONDS
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            with _suppress_process_errors():
                os.killpg(pgid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False  # already gone
    except OSError as e:
        logger.warning("stop_campaign: killpg failed for %s: %s", campaign_id, e)
        return False


def _cluster_cooperative_stop(campaign_id, entry):
    """Send the cooperative ``stop`` command to the in-cluster controller."""
    from robovast.execution.cluster_execution import control_client
    from robovast.execution.cluster_execution.cluster_setup import get_cluster_namespace

    context = entry.get("context") or None
    namespace = get_cluster_namespace(context)
    pod, _phase = control_client.find_controller_pod(
        namespace=namespace, kube_context=context, campaign=campaign_id)
    if pod is None:
        raise RuntimeError(f"no controller pod found for {campaign_id!r}")
    with control_client.port_forward(
            pod, namespace=namespace, kube_context=context) as base_url:
        control_client.send_command(base_url, "stop")


def list_running_campaigns() -> dict:
    """List campaigns currently tracked as live (both local and cluster).

    Liveness is derived from the launched process registry — no Kubernetes call
    is made, so this never blocks on an unreachable cluster. For in-cluster
    detail beyond the local waiter's state, use ``vast exec cluster monitor``.

    Returns:
        ``{count, running: [entry, ...]}`` where each entry carries at least
        ``campaign_id``, ``backend``, and ``status``.
    """
    try:
        client = _service_client()
        if client is not None:
            resp = client.list_campaigns()
            terminal = {"finished", "failed", "crashed", "unknown"}
            running = [{"campaign_id": c.campaign_id, "backend": "service",
                        "status": c.phase}
                       for c in resp.campaigns if c.phase not in terminal]
            return {"count": len(running), "running": running}

        project = _load_project()
        results_dir = str(project.results_dir)
        registry = _registry(results_dir)
        _reap_local_procs(registry)
        running = registry.live_entries()
        return {"count": len(running), "running": running}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# -- Small utilities ---------------------------------------------------------


class _suppress_process_errors:
    """Context manager swallowing process/OS errors from best-effort kills."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(
            exc_type, (ProcessLookupError, OSError, subprocess.SubprocessError))


# -- Plugin class ------------------------------------------------------------

def get_postprocessing(campaign_id: str) -> dict:
    """Show a campaign's effective analysis-postprocessing entries + edit history.

    Raw rosbags are always preserved, so postprocessing can be edited and re-run
    to compute *different* metrics later without re-executing the campaign. The
    immutable ``_config/`` snapshot is never changed; edits are versioned
    overrides. Pair with :func:`update_postprocessing` + :func:`run_postprocessing`.

    Returns:
        ``{campaign_id, source, entries, revisions}`` or ``{error}``.
    """
    from robovast.service.client import RobovastClient
    try:
        return RobovastClient(os.environ.get("ROBOVAST_SERVICE_URL", "")) \
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
    from robovast.service.client import RobovastClient
    from robovast.service.interface import UpdatePostprocessingRequest
    try:
        return RobovastClient(os.environ.get("ROBOVAST_SERVICE_URL", "")) \
            .update_postprocessing(UpdatePostprocessingRequest(
                campaign_id=campaign_id, entries=entries)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def run_postprocessing(campaign_id: str, force: bool = False,
                       skip: list | None = None) -> dict:
    """(Re)run analysis postprocessing for one campaign with its effective config.

    Reprocesses just this campaign (not its siblings), rebuilding ``data.db`` so
    the new metrics are immediately queryable via ``query_campaign_data_sql``.

    Args:
        campaign_id: The campaign to (re)process.
        force: Bypass per-rosbag caches and reprocess all bags.
        skip: Plugin names to skip (e.g. ``["rosbags_to_webm"]``).
    """
    from robovast.service.client import RobovastClient
    from robovast.service.interface import RunPostprocessingRequest
    try:
        return RobovastClient(os.environ.get("ROBOVAST_SERVICE_URL", "")) \
            .run_postprocessing(RunPostprocessingRequest(
                campaign_id=campaign_id, force=force, skip=skip or [])).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


_TOOLS = [
    validate_project,
    preview_configurations,
    init_project,
    start_campaign,
    get_campaign_status,
    stop_campaign,
    list_running_campaigns,
    get_postprocessing,
    update_postprocessing,
    run_postprocessing,
]


class CampaignControlPlugin:
    """Expose campaign control (validate/start/status/stop/list) as MCP tools."""

    name = "campaign_control"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
