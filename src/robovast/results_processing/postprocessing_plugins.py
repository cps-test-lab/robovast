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
import contextlib
import csv
import dataclasses
import glob
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Dict, List, Optional, Tuple


from robovast.common import log_summary, scenario_markers
from robovast.common.campaign_data import PROBE_DIR
from robovast.common.execution import (COMPAT_VERSION, MIN_IMAGE_COMPAT,
                                       is_campaign_dir)
from robovast.common.quantity import to_bytes, to_cores
from robovast.common.results_utils import campaign_vast
from robovast.common.stop import terminate_group, watch_stop

logger = logging.getLogger(__name__)


def _campaign_config_path(results_dir: str, config_dir: str):
    """The ``.vast`` describing *results_dir*, or ``None`` when there is none to read.

    The campaign's own frozen config comes first: it is the single source of truth for what
    ran, it is the file the cluster lane reads, and it is what a re-run dialog edits -- so
    preferring it keeps every reader answering from the same place. A campaign tree need not
    hold one, though. Results are projected into it by a step that may not have run, and this
    plugin is also called against a directory that is no campaign tree at all; then
    *config_dir* answers, being the directory holding the ``.vast`` being executed, which is
    why it is a parameter at all.

    ``None`` rather than an exception when neither has one: what this feeds is an optional
    block, so its absence is an answer -- the shared defaults -- and not a missing input.
    """
    try:
        return str(campaign_vast(results_dir))
    except (ValueError, OSError):
        pass
    candidates = sorted(glob.glob(os.path.join(config_dir or ".", "*.vast")))
    return candidates[0] if len(candidates) == 1 else None


def _docker_cpus(cpu) -> str:
    """A cpu declaration as ``docker run --cpus`` wants it (``"500m"`` -> ``"0.5"``).

    Docker takes a decimal core count and rejects Kubernetes' millicore spelling. An
    unparseable value passes through so docker's own error names it, since the config
    layer has already refused those.
    """
    cores = to_cores(cpu)
    if cores is None:
        return str(cpu)
    return str(int(cores)) if float(cores).is_integer() else str(cores)


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

    #: What one run's output may depend on.
    #:
    #: ``"run"``: only that run's own directory, the scenario jobs it links, the other runs
    #: of those jobs (a packed job's timeline is shared out between its runs), and the
    #: campaign-level inputs (``_config``, ``_execution``, ``_transient``). Such a step can
    #: be run on a campaign tree holding any set of whole jobs with their runs, and the union
    #: of the outputs is the whole campaign's -- which is what lets a cluster split it
    #: across Jobs.
    #:
    #: ``"campaign"``: anything, so it runs once over the whole tree. The default: a step
    #: that says nothing is never split, and costs only the speed-up.
    scope: str = "campaign"

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

        # The cluster lane's staging initContainer restores this bit for the `_config/`
        # subtree a `command` step's script lives in (see `postprocess_stage.py`; the rest
        # of a campaign's tree -- bags and CSVs, orders of magnitude larger -- skips that
        # restoration on purpose, since nothing reads them as a program). This is a second,
        # narrower line of defence for every other way a staged script can still arrive
        # non-executable: the local (non-cluster) lane never runs that initContainer at
        # all, and a workspace push from a filesystem or transport that does not preserve
        # POSIX modes would land here the same way. Cheap either way -- one stat/chmod pair
        # -- and this is the one caller that actually execve()s the file.

        if os.path.isfile(script_path) and not os.access(script_path, os.X_OK):
            try:
                mode = os.stat(script_path).st_mode
                os.chmod(script_path, mode | 0o111)
            except OSError:
                pass  # fall through; the exec below reports the real error if this didn't help

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

    ``rosbags_process`` follows it for every unreadable bag, whoever cut the job off; this
    list is the narrower question of which of them somebody *chose*, so its summary can
    separate the expected gaps from the merely observed ones. Any future step that *scans*
    rather than iterating the run table should consult this rather than inventing a second
    answer.

    Two kinds qualify, for one reason. ``killed``: an operator stopped the job by hand.
    ``invalid``: the runner threw the trial away because a container it depended on crashed
    and was restarted under it. Both end with ``delete_namespaced_job`` at
    ``grace_period_seconds=0``, i.e. a recorder SIGKILLed mid-write and a bag that can never
    be opened — so treating them differently here would reintroduce exactly the failure the
    rule above exists to prevent, one layer down: one invalidated trial costing the metrics
    of every job in the campaign that did finish.

    ``[]`` for every campaign nobody intervened in — the ledger file does not exist — so
    this costs one missing-file check on the normal path and changes nothing about it.

    Read here rather than passed in because the postprocessing pipeline runs against a
    campaign directory (on the cluster, the copy a postprocessing pod fetched), and its
    plugins take their inputs from ``results_dir``; there is no caller in that process
    holding the kill.
    """
    from robovast.common.campaign_data import (KIND_INVALID, KIND_KILLED,
                                                read_interventions)
    try:
        entries = read_interventions(results_dir)
    except Exception:  # noqa: BLE001 - never let the ledger break postprocessing
        return []
    return sorted({e["job_dir"] for e in entries
                   if e.get("job_dir") and e.get("kind") in (KIND_KILLED, KIND_INVALID)})


#: Where a ``rosbags_process`` entry looks for bags when it names none.
DEFAULT_BAG_DIR = "rosbag2"


def conversion_groups(plugins: Optional[List[dict]] = None, bag_dir: Optional[str] = None,
                      groups: Optional[List[dict]] = None) -> List[dict]:
    """The ``groups`` a ``rosbags_process`` entry converts, from either way it is written.

    ``plugins`` (with an optional ``bag_dir``) is one group, as a ``.vast`` entry writes it;
    ``groups`` is several, each ``{"bag_dir": …, "plugins": […]}``, as the orchestrator
    passes a campaign's combined entries. Every runner builds the script's ``--config``
    from this, so an entry means the same thing wherever it runs.

    Raises ``ValueError`` when both or neither are given, or ``bag_dir`` is given with
    ``groups`` -- an argument that would otherwise be dropped.
    """
    if groups is not None:
        if plugins is not None or bag_dir is not None:
            raise ValueError("rosbags_process takes either 'groups' or 'plugins' "
                             "(with an optional 'bag_dir'), not both")
        if not groups:
            raise ValueError("rosbags_process 'groups' is empty")
        return [dict(group) for group in groups]
    if not plugins:
        raise ValueError("rosbags_process requires at least one entry under 'plugins'")
    return [{"bag_dir": bag_dir or DEFAULT_BAG_DIR, "plugins": list(plugins)}]


#: The directory every image step's command runs from: the conversion scripts, and whatever
#: an :class:`ExecutionImagePlugin` ships beside them. The same path wherever it runs --
#: ``docker_exec.sh`` mounts it, a cluster Job mounts its ConfigMap there.
IMAGE_SCRIPTS_DIR = "/scripts"


@dataclasses.dataclass(frozen=True)
class ImageStep:
    """One step as a lane runs it in the execution image: its command and its files."""

    #: The postprocessing entry's plugin name, for the log and for errors.
    name: str
    #: A file in the scripts directory, then its arguments.
    argv: List[str]
    #: What :meth:`ExecutionImagePlugin.image_files` ships beside the scripts.
    files: List = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class ImageContext:
    """What an image step is told about where it runs, as the container sees it.

    The lane builds this; the plugin reads it to write its command, so one plugin runs on a
    developer's Docker and in a cluster Job without knowing which.
    """

    #: The campaign directory inside the container.
    campaign_dir: str
    #: Where the step may record provenance JSON inside the container, or ``None``.
    provenance_file: Optional[str] = None
    #: Redo work a step would otherwise find already done.
    force: bool = False
    #: Print per-item detail rather than progress only.
    debug: bool = False
    #: Campaign-relative job directories whose output was cut short by a stop or an
    #: invalidation -- see :func:`_interrupted_job_dirs`. Missing data there is expected.
    tolerate_under: Tuple[str, ...] = ()


class ExecutionImagePlugin(BasePostprocessingPlugin):
    """A postprocessing step that runs **inside the campaign's own execution image**.

    Some work can only happen there: deserializing a bag needs the message definitions the
    run recorded with, and a custom type exists only in the image that defined it. Such a
    step is not Python this package runs; it is a command this package runs *in* that
    image. A subclass says which with :meth:`image_command`, and every lane runs exactly
    that command:

    * ``vast results postprocess`` on a development machine, through ``docker_exec.sh``
      (this class's :meth:`__call__`);
    * a cluster postprocessing Job, directly in its execution-image container.

    The command runs from :data:`IMAGE_SCRIPTS_DIR`, which holds the conversion scripts of
    ``robovast.results_processing.data`` and every file :meth:`image_files` names. Nothing
    there may import ``robovast``: the image does not have it.
    """

    needs_execution_image = True

    #: A line of the command's output that starts with this is progress, redrawn in place
    #: rather than printed once per update. ``None``: every line is printed.
    progress_prefix: Optional[str] = None

    def image_command(self, ctx: ImageContext, **params) -> List[str]:
        """The command to run in the image for *params*: a file in the scripts directory,
        then its arguments.

        Raises ``ValueError`` for parameters it cannot follow, and ``TypeError`` for one it
        does not take -- never drops one.
        """
        raise NotImplementedError(
            f"{type(self).__name__} needs the execution image but names no image_command")

    def image_files(self) -> List:
        """Files, beyond the conversion scripts, this step's command needs beside it.

        Each is an ``importlib.resources`` traversable or a path, placed in the scripts
        directory under its own name. Stdlib and what the image carries only, like the
        scripts themselves.
        """
        return []

    def __call__(self, results_dir: str, config_dir: str,
                 provenance_file: Optional[str] = None,
                 execution_image: Optional[str] = None,
                 debug: bool = False, force: bool = False, should_stop=None,
                 **params) -> Tuple[bool, str]:
        """Run :meth:`image_command` in the execution image through ``docker_exec.sh``.

        *should_stop* is polled while the command runs; when it turns true the container is
        torn down and this returns a stated cancellation. An image step is written to be
        interruptible: it rewrites its outputs rather than appending, so the item it was on
        is redone next time.
        """
        name = type(self).__name__
        ctx = ImageContext(
            campaign_dir=_CONTAINER_INPUT,
            provenance_file=(f"/provenance/{os.path.basename(provenance_file)}"
                             if provenance_file else None),
            force=force, debug=debug,
            tolerate_under=tuple(_interrupted_job_dirs(results_dir)))
        try:
            argv = self.image_command(ctx, **params)
        except ValueError as e:
            return False, f"{name}: {e}"

        with _image_scripts_dir(self.image_files()) as scripts_dir:
            cmd = [os.path.join(scripts_dir, "docker_exec.sh"),
                   "--compat-version", str(COMPAT_VERSION),
                   "--min-compat-version", str(MIN_IMAGE_COMPAT),
                   "--input", results_dir]
            if execution_image:
                cmd.extend(["--image", execution_image])
            if provenance_file:
                cmd.extend(["--provenance-file", provenance_file])
            # What the step may use, from the campaign's `results_processing.resources` over
            # the shared defaults -- the same figure the cluster lane reserves for its image
            # container, so one block means one thing wherever it runs.
            from robovast.results_processing.postprocessing import (  # noqa: PLC0415
                postprocess_convert_resources)
            sized = postprocess_convert_resources(
                _campaign_config_path(results_dir, config_dir))
            cmd.extend(["--cpus", _docker_cpus(sized["cpu"]),
                        "--memory", str(to_bytes(sized["memory"]))])
            cmd.extend(argv)
            return self._stream(cmd, scripts_dir, debug, should_stop)

    def _stream(self, cmd: List[str], cwd: str, debug: bool, should_stop) -> Tuple[bool, str]:
        """Run *cmd*, echoing its output as it comes; ``(ok, summary or failure)``."""
        name = type(self).__name__
        process = None
        try:
            # In a session of its own so the step can be signalled as one group -- see
            # :func:`~robovast.common.stop.terminate_group`, which is the only thing that
            # can end it early. The
            # cost of that session is that Ctrl+C no longer arrives here for free, since
            # this is no longer in the terminal's foreground group; the KeyboardInterrupt
            # branch below forwards it, so an interactive run tears the container down.
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr into stdout to avoid deadlock
                text=True,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'},
                start_new_session=True,
            )
            output_lines: List[str] = []
            last_was_progress = False
            # A second's latency on a step that runs for minutes to hours is free, and
            # the grace is what lets ``docker_exec.sh``'s trap kill and remove the
            # container rather than leaving it to the daemon's reaping.
            with watch_stop(should_stop, process, poll=1.0, grace_s=15.0):
                for line in process.stdout:
                    line = line.rstrip("\n")
                    output_lines.append(line)
                    is_progress = bool(self.progress_prefix
                                       and line.startswith(self.progress_prefix))
                    if is_progress and not debug:
                        print(f"\r{line}", end="", flush=True)
                    else:
                        if last_was_progress and not debug:
                            print()
                        print(line, flush=True)
                    last_was_progress = is_progress
                if last_was_progress and not debug:
                    print()
                returncode = process.wait()
            output = "\n".join(output_lines)
            if returncode != 0:
                if should_stop is not None and should_stop():
                    # Named as what it was. The exit code of a step we killed says
                    # "signalled", and reporting that as a failure would file the operator's
                    # own stop under faults.
                    return False, (f"{name} cancelled: the campaign was stopped. Its inputs "
                                   "are untouched and the item it was on is redone when "
                                   "postprocessing is re-run.")
                return False, f"{name} failed with exit code {returncode}\n{output}"
            summary = next((line for line in output_lines if line.startswith("Summary:")),
                           next((line for line in reversed(output_lines) if line.strip()),
                                f"{name} completed"))
            return True, summary
        except KeyboardInterrupt:
            # Only reachable because of the new session above: the interrupt reaches this
            # process but no longer the step's group, so without forwarding it an
            # interactive Ctrl+C would leave a container running with nobody reading it.
            if process is not None:
                terminate_group(process)
            raise
        except Exception as e:  # pylint: disable=broad-except
            return False, f"Error executing {name}: {e}"


#: Where ``docker_exec.sh --input`` mounts the campaign directory.
_CONTAINER_INPUT = "/input"


@contextlib.contextmanager
def _image_scripts_dir(extra_files):
    """The directory an image step runs from: the packaged scripts, plus *extra_files*.

    The packaged directory itself when a step ships nothing of its own; otherwise a
    temporary copy with the extra files beside the scripts, since ``docker_exec.sh`` mounts
    the directory it lives in.
    """
    packaged = files("robovast.results_processing.data")
    if not extra_files:
        yield str(packaged)
        return
    with tempfile.TemporaryDirectory(prefix="robovast_image_step_") as staged:
        for entry in packaged.iterdir():
            if entry.is_file():
                target = os.path.join(staged, entry.name)
                Path(target).write_bytes(entry.read_bytes())
                shutil.copymode(str(entry), target)
        for extra in extra_files:
            target = os.path.join(staged, Path(str(extra)).name)
            if os.path.exists(target):
                raise ValueError(f"image file {Path(str(extra)).name!r} would replace a "
                                 "conversion script of the same name")
            Path(target).write_bytes(extra.read_bytes() if hasattr(extra, "read_bytes")
                                     else Path(extra).read_bytes())
        yield staged


class RosbagsProcess(ExecutionImagePlugin):
    # Reads rosbags, so it needs the image whose message definitions wrote them.
    # One bag at a time, each written beside itself.
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

    ``plugins`` converts the bags in ``bag_dir`` (default ``rosbag2``). The orchestrator
    combines every ``rosbags_process`` entry and ``rosbags_*`` name of a campaign into one
    call, one group per bag directory, and adds the ``logs/rosout_bag`` handlers to it
    (``_batch_rosbags_commands``), so every kind of bag is converted in one scan and one
    worker pool. That combined call is what arrives here as ``groups``.

    **How much of the machine it uses is not set here.** The step converts one bag per
    process, and how many run at once follows the CPU the conversion is allowed -- which is
    ``results_processing.resources`` (see
    :class:`~robovast.common.config.PostprocessResourcesConfig`), one block that sizes both
    lanes. Left alone, a conversion takes the shared default rather than a worker per core of
    whatever machine it landed on::

        results_processing:
          resources:
            cpu: 8
            memory: 16Gi

    ``workers`` below overrides just the fan-out, for a conversion whose bags are large
    enough that fewer, fatter workers beat one per core.
    """

    progress_prefix = "Processing rosbags"
    scope = "run"

    def image_command(self, ctx: ImageContext,  # pylint: disable=arguments-differ
                      plugins: Optional[List[dict]] = None,
                      workers: Optional[int] = None, bag_dir: Optional[str] = None,
                      groups: Optional[List[dict]] = None) -> List[str]:
        """``rosbags_process.py`` over the campaign, with the groups this entry names.

        Args:
            plugins: List of handler config dicts, each with a ``type`` key, for the bags in
                *bag_dir*. Give either this or *groups*.
            workers: Bags to convert at once. Omitted -- the normal case -- the conversion
                derives it from the CPU it is actually allowed (its cgroup quota), so it
                matches ``results_processing.resources.cpu`` wherever it runs. Set it only to
                override that.
            bag_dir: Rosbag subdirectory name to search for (default: "rosbag2").
            groups: Several ``{"bag_dir": …, "plugins": […]}`` converted in one pass: what the
                orchestrator passes after combining a campaign's entries.
        """
        argv = ["rosbags_process.py",
                "--config", json.dumps({"groups": conversion_groups(plugins, bag_dir, groups)}),
                # A calibration probe is deliberately not a run, so its bag is not campaign
                # data. Only this directory, NOT every reserved one:
                # `_jobs/<batch>/<job>/logs/rosout_bag` is each job's real log bag.
                "--skip-dir", PROBE_DIR]
        if ctx.provenance_file:
            argv += ["--provenance-file", ctx.provenance_file]
        if workers is not None:
            argv += ["--workers", str(int(workers))]
        # A job stopped by hand or invalidated by the runner was SIGKILLed mid-write, so its
        # rosbag is unfinalized and cannot be opened, ever -- expected, not a failure.
        for job_dir in ctx.tolerate_under:
            argv += ["--tolerate-under", job_dir]
        if ctx.debug:
            argv.append("--debug")
        if ctx.force:
            argv.append("--force")
        argv.append(ctx.campaign_dir)
        return argv


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
    once a job has finished. The output lands in the **run** directory, where the index
    ingest's glob already looks, so the ``run_log`` table needs no ingest code of its own.

    Example usage in .vast config (only needed to override a default):

    .. code-block:: yaml

       postprocessing:
         - run_log:
             min_severity: warn
    """

    scope = "run"

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
                (``execution_image`` is set by the cluster Job and not by
                ``docker_exec.sh``). Naming a subset therefore passes with one runner and
                raises ``unexpected keyword argument`` with the other, which is exactly how
                this plugin first failed on the cluster. This plugin needs none of them: it reads the campaign's own
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
    symlink, which only appears once a job has finished. The output lands in the **run**
    directory, where the index ingest's glob already looks, so the ``resource_usage``
    table needs no ingest code of its own.

    Unlike ``run_log``, a job's samples are **partitioned** between the runs it served
    rather than given to all of them: another run's CPU is not this run's, and copying it
    would make every aggregate over a packed campaign report a multiple of the truth.

    Takes no parameters. Every filter one might add (a container, a severity, a decimation)
    is a ``WHERE`` clause the reader already has, and one applied at write time cannot be
    undone without re-running postprocessing.
    """

    scope = "run"

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
                (``execution_image`` is set by the cluster Job and not by
                ``docker_exec.sh``). Naming a subset therefore passes with one runner and
                raises ``unexpected keyword argument`` with the other, which is how
                ``RunLog`` first failed on the cluster. This plugin needs none of them.

        Returns:
            Tuple of (success, message, provenance entries).
        """
        from robovast.common.campaign_data import \
            campaign_container_plan  # pylint: disable=import-outside-toplevel

        from . import (resource_usage, run_slices,  # pylint: disable=import-outside-toplevel
                       system_usage)

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
                # The container-level sibling rides along here rather than in a plugin of
                # its own: one daemon writes both files, so one step should slice both. A
                # separate plugin would need its own entry point and its own opt-in, and a
                # campaign that sampled the counters but never declared it would silently
                # produce no table.
                sys_columns, sys_samples = system_usage.collect_job_rows(slice_.job_dir)
                job_cache[slice_.job_dir] = (ticks, sources, sys_columns, sys_samples)
                totals.add_job(stats)
            ticks, sources, sys_columns, sys_samples = job_cache[slice_.job_dir]

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

            # Written even with no columns, so the table's presence says the sampler ran and
            # its emptiness says this runtime reports nothing -- which a missing file cannot
            # tell apart from a step that never executed.
            sys_rows = system_usage.rows_for_slice(sys_columns, sys_samples, slice_)
            sys_output = slice_.run_dir / system_usage.FILENAME
            run_slices.write_csv(str(sys_output), system_usage.fieldnames(sys_columns),
                                 sys_rows)
            entries.append({
                "output": os.path.relpath(sys_output, root),
                "sources": sorted(
                    os.path.relpath(os.path.join(slice_.job_dir, name), root)
                    for name in os.listdir(slice_.job_dir)
                    if name.startswith(system_usage.CSV_PREFIX) and name.endswith(".csv")
                ) if os.path.isdir(slice_.job_dir) else [],
                "plugin": "system_usage",
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


#: Postgres truncates an identifier past this many bytes, silently. So a table name over
#: the limit is not a long name, it is a *different* one -- and two data files whose names
#: agree up to the cut become one table, whose rows are the two files' appended together
#: with nothing raised. The same reality :data:`~robovast.results_processing.
#: campaign_ingest._MAX_COLUMN_BYTES` states for columns.
_MAX_TABLE_NAME_BYTES = 63

#: How many hex characters of the full name's hash to keep when a table name is shortened.
#: Long enough that two shortened names colliding by chance is not a real concern, short
#: enough to leave most of the budget to the readable head.
_TABLE_NAME_HASH_LEN = 8


def _csv_to_table_name(filename: str) -> str:
    """Convert a data filename to a table name the index can actually hold.

    Strips the .csv/.jsonl extension, replaces non-alphanumeric/underscore characters
    with underscores, lowercases, and prefixes with 't_' if it starts with a digit.

    A name past :data:`_MAX_TABLE_NAME_BYTES` is shortened and given a hash of the whole
    sanitised name, because Postgres would otherwise truncate it *for* us and two files
    would silently share a table. Hashing the full name and not the kept head is the part
    that matters: a bag-derived name is the recording's directory plus the topic, so two
    topics under one long prefix agree in exactly the characters a bare truncation keeps.
    The head is kept only so the table is recognisable to someone reading a table list;
    ``_table_name_map`` is what a reader resolves a file to its table through.

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
    sanitized = sanitized or "t_unknown"
    if len(sanitized.encode()) <= _MAX_TABLE_NAME_BYTES:
        return sanitized
    digest = hashlib.sha256(sanitized.encode()).hexdigest()[:_TABLE_NAME_HASH_LEN]
    head = sanitized[: _MAX_TABLE_NAME_BYTES - 1 - _TABLE_NAME_HASH_LEN].rstrip("_")
    return f"{head}_{digest}"


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
            # A `#` preamble before the header is how a producer states what its columns
            # mean -- the frame a wrench is in, the unit of a column -- and numpy and pandas
            # both read past it. Taken as the header instead, its comma-split words became
            # the columns and every real row was ragged.
            reader = csv.DictReader(line for line in handle if not line.startswith("#"))
            rows = list(reader)
    except Exception:  # pylint: disable=broad-except
        return []
    if any(None in row for row in rows):
        # A row longer than the header. DictReader files the surplus under the key None,
        # which no column can be named after, and the one file used to take the whole
        # campaign's index down with it -- from a TypeError in a sort, naming nothing.
        logger.warning(
            "index: skipping %s: a row has more fields than its header (%d columns). Its "
            "rows are not indexed; every other file still is.", path, len(reader.fieldnames or ()))
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
