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

"""Postprocessing functionality for run result data."""
import inspect
import json
import logging
import os
import re
import tempfile
from importlib.metadata import entry_points
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from robovast.common.common import load_config
from robovast.common.plugin_ref import is_file_ref, load_ref
from robovast.common.results_utils import find_campaign_vast_file
from robovast.results_processing.metadata import generate_campaign_metadata

POSTPROCESSING_GROUP = "robovast.postprocessing_commands"


def load_postprocessing_plugins() -> Dict[str, callable]:
    """Load postprocessing command plugins from entry points.

    All plugins must be classes that inherit from
    :class:`~robovast.results_processing.postprocessing_plugins.BasePostprocessingPlugin`.
    Class-based plugins are automatically instantiated so that callers always
    receive a ready-to-use callable.  Class instances additionally expose
    :meth:`~robovast.results_processing.postprocessing_plugins.BasePostprocessingPlugin.get_files_to_copy`
    which is used during config preparation to copy required files into
    ``_config/``.

    Returns:
        Dictionary mapping plugin names to their callable objects (class instances).
    """
    plugins = {}
    try:
        eps = entry_points(group='robovast.postprocessing_commands')
        for ep in eps:
            try:
                # Load the entry point - must be a class
                plugin_obj = ep.load()
                if not inspect.isclass(plugin_obj):
                    print(f"Warning: Postprocessing plugin '{ep.name}' is not a class and will be skipped. "
                          f"All plugins must be classes inheriting from BasePostprocessingPlugin.")
                    continue
                # Instantiate class-based plugins so callers get a consistent
                # callable interface and can also access get_files_to_copy.
                plugin_obj = plugin_obj()
                plugins[ep.name] = plugin_obj
            except Exception as e:
                # Log and continue if a plugin fails to load
                print(f"Warning: Failed to load postprocessing plugin '{ep.name}': {e}")
    except Exception:
        # No plugins available or entry_points call failed
        pass
    return plugins


def resolve_postprocessing_plugin(plugin_name: str, config_dir: str,
                                  plugins: Optional[Dict[str, callable]] = None) -> callable:
    """Resolve a postprocessing plugin by entry-point name OR local file ref.

    Both postprocessing lists (``results_processing.postprocessing`` and
    ``search.postprocessing``) load plugins identically: a ``plugin_name`` is
    either an entry-point name (``robovast.postprocessing_commands``) or a local
    ``<path>.py:<Class>`` file reference resolved relative to *config_dir*. File
    refs let a SUT ship its own postprocessing plugin without packaging it.

    Returns a ready-to-use callable (class instances are instantiated). Raises
    ``KeyError``/``ValueError`` if the plugin cannot be resolved.
    """
    if is_file_ref(plugin_name):
        cls = load_ref(plugin_name, POSTPROCESSING_GROUP, config_dir)
        plugin = cls() if inspect.isclass(cls) else cls
        # A postprocessing plugin is invoked by calling it (BasePostprocessingPlugin
        # via __call__, or a plain callable). Reject anything else up front so the
        # error is reported at load/validation time rather than mid-run.
        if not callable(plugin):
            raise ValueError(
                f"Postprocessing plugin '{plugin_name}' is not callable; it must be "
                f"a BasePostprocessingPlugin subclass or a callable.")
        return plugin
    if plugins is None:
        plugins = load_postprocessing_plugins()
    if plugin_name not in plugins:
        available = ', '.join(sorted(plugins.keys())) or 'none'
        raise KeyError(
            f"Unknown postprocessing plugin: '{plugin_name}'. Available: {available}. "
            f"Use an entry-point name or a './path.py:Class' local file reference.")
    return plugins[plugin_name]


def run_postprocessing_commands(commands, results_dir: str, config_dir: str,
                                output=print, execution_image: Optional[str] = None,
                                debug: bool = False, force: bool = False
                                ) -> Tuple[bool, List[dict]]:
    """Resolve and run a list of postprocessing commands over *results_dir*.

    Shared by ``run_postprocessing`` (the ``results_processing.postprocessing``
    path) and the campaign controller (the ``search.postprocessing`` path), so
    both load plugins identically (entry-point name or local file ref) and apply
    the same execution contract. Returns ``(success, provenance_entries)``.
    """
    plugins = load_postprocessing_plugins()
    success = True
    entries: List[dict] = []
    with tempfile.TemporaryDirectory(prefix="robovast_provenance_") as temp_dir:
        for i, command in enumerate(commands, 1):
            if isinstance(command, str):
                plugin_name, params = command, {}
            elif isinstance(command, dict) and len(command) == 1:
                plugin_name = next(iter(command))
                params = command[plugin_name] or {}
            else:
                output(f"[{i}/{len(commands)}] ✗ Invalid command format: {command!r}")
                success = False
                continue
            try:
                plugin_func = resolve_postprocessing_plugin(plugin_name, config_dir, plugins)
            except (KeyError, ValueError, ImportError, FileNotFoundError, AttributeError) as e:
                output(f"[{i}/{len(commands)}] ✗ {e}")
                success = False
                continue
            output(f"[{i}/{len(commands)}] Executing: {plugin_name}")
            ok, message, prov = execute_postprocessing_plugin(
                plugin_name=plugin_name, plugin_func=plugin_func, params=params,
                results_dir=results_dir, config_dir=config_dir,
                provenance_file=os.path.join(temp_dir, f"{i}_provenance.json"),
                execution_image=execution_image, debug=debug, force=force)
            entries.extend(prov)
            if not ok:
                output(f"✗ {message}")
                success = False
            else:
                output(f"✓ {message.splitlines()[0] if message else 'done'}")
    return success, entries


def execute_postprocessing_plugin(
    plugin_name: str,
    plugin_func: callable,
    params: dict,
    results_dir: str,
    config_dir: str,
    provenance_file: Optional[str] = None,
    execution_image: Optional[str] = None,
    debug: bool = False,
    force: bool = False,
) -> Tuple[bool, str, List[dict]]:
    """Execute a postprocessing plugin with parameters.

    Args:
        plugin_name: Name of the plugin
        plugin_func: The plugin function to call
        params: Dictionary of parameters for the plugin
        results_dir: Path to the campaign-<id> directory
        config_dir: Directory containing the configuration file
        provenance_file: Optional path for container plugins to write provenance JSON
        execution_image: Optional Docker image from the execution phase

    Returns:
        Tuple of (success, message, provenance_entries)
    """
    kwargs = {
        'results_dir': results_dir,
        'config_dir': config_dir,
        **params,
    }
    if provenance_file is not None:
        kwargs['provenance_file'] = provenance_file
    if execution_image is not None:
        kwargs['execution_image'] = execution_image
    if debug:
        kwargs['debug'] = debug
    if force:
        kwargs['force'] = force

    try:
        result = plugin_func(**kwargs)
        if isinstance(result, (list, tuple)) and len(result) >= 3:
            success, message, entries = result[0], result[1], result[2]
            return success, message, entries if isinstance(entries, list) else []
        if isinstance(result, (list, tuple)) and len(result) >= 2:
            success, message = result[0], result[1]
        else:
            success, message = result
        # Collect provenance from container-written file if present
        entries = []
        if provenance_file and os.path.isfile(provenance_file):
            try:
                with open(provenance_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    entries = data.get('entries', [])
            except (json.JSONDecodeError, OSError):
                pass
        return success, message, entries
    except TypeError as e:
        return False, f"Plugin '{plugin_name}' argument error: {e}", []
    except Exception as e:
        return False, f"Plugin '{plugin_name}' execution error: {e}", []


# Plugin names that can be transparently batched into a single rosbags_process call.
# Maps plugin name → (handler_type, default_bag_dir).
_ROSBAG_BATCH_MAP: Dict[str, Tuple[str, str]] = {
    "rosbags_to_csv":        ("to_csv",         "rosbag2"),
    "rosbags_tf_to_csv":     ("tf_to_csv",       "rosbag2"),
    "rosbags_nav2bt_to_csv": ("nav2_bt_to_csv",  "rosbag2"),
    "rosbags_action_to_csv": ("action_to_csv",   "rosbag2"),
    "rosbags_rosout_to_csv": ("rosout_to_csv",   "logs/rosout_bag"),
    "rosbags_clock_to_csv":  ("clock_to_csv",    "logs/rosout_bag"),
    "rosbags_costmap_to_csv": ("costmap_to_csv", "rosbag2"),
    "rosbags_to_webm":       ("to_webm",         "rosbag2"),
}

#: Handlers on the *infrastructure* bag (``logs/rosout_bag``, recorded by the entrypoint in
#: wall time for the whole container's life), auto-injected for every campaign because both
#: answer questions no campaign should have to ask for: what the run said, and how its wall
#: clock relates to sim time. Each is still skippable by name (``skip=[…]``).
_AUTO_INFRA_HANDLERS: Tuple[str, ...] = ("rosbags_rosout_to_csv", "rosbags_clock_to_csv")

#: Postprocessing command names that are not entry points but are transparently
#: rewritten into a batched ``rosbags_process`` call at runtime (see
#: :func:`_batch_rosbags_commands`). Validation must treat these as valid too,
#: otherwise it rejects configs the runtime would happily execute.
ROSBAG_BATCH_NAMES: frozenset = frozenset(_ROSBAG_BATCH_MAP)

#: Everything the cluster lane's postprocessing **Job** runs, and therefore everything the in-pod
#: pass must skip. The aliases above plus ``rosbags_process`` itself, which a ``.vast`` may name
#: directly (:class:`~robovast.results_processing.postprocessing_plugins.RosbagsProcess` documents
#: that spelling) and which ``postprocess_job.rosbag_commands_for`` already collects for the Job.
#:
#: Without ``rosbags_process`` here the two sides disagree and a directly-authored call runs TWICE:
#: once in the Job, correctly, and once in the controller pod through ``docker_exec.sh`` -- which
#: shells out to ``docker run`` and cannot work there, so the compat check reads an empty string and
#: the campaign fails with "container image provides: <missing>" against an image that carries the
#: file. The data is already correct by then, which is what makes the failure so misleading.
ROSBAG_JOB_NAMES: frozenset = ROSBAG_BATCH_NAMES | {"rosbags_process"}

#: Commands that register a video in a run's ``videos`` table (``rosbags_process.VIDEOS_CSV``),
#: which is what the run view's ``camera`` panel and ``get_camera_frame`` read.
#:
#: A set rather than one name because that manifest is a **contract**, not this step's private
#: file: a producer that renders its own video joins by writing the same row and adding itself
#: here. Validation reads this to tell a campaign that declares a camera panel and nothing to
#: fill it, which is otherwise only discovered once the compute is spent.
VIDEO_PRODUCER_COMMANDS: frozenset = frozenset({"rosbags_to_webm"})


#: Plugins that run for every campaign without being declared, appended after the
#: (batched) rosbag conversions because they read what those produce — ``run_log`` needs
#: ``rosout.csv`` and both need the ``clock_map.csv`` that ``rosbags_clock_to_csv`` writes.
#: Skippable by name, like any other command.
AUTO_PLUGINS: Tuple[str, ...] = ("run_log", "resource_usage")


def _append_auto_plugins(commands: List, skip: "set | None" = None) -> List:
    """Append :data:`AUTO_PLUGINS` not already present and not skipped.

    A campaign that declares one itself keeps its own parameters and its own position in
    the order, rather than getting a second, default-configured copy.
    """
    skip_names = set(skip or ())
    declared = {c if isinstance(c, str) else list(c.keys())[0] for c in commands}
    return list(commands) + [name for name in AUTO_PLUGINS
                             if name not in declared and name not in skip_names]


#: What a cause-shaped line looks like. A failing step's first line is usually its exit status
#: ("rosbags_process failed with exit code 1"), which says THAT it failed and nothing about why;
#: the reason is further down, in the tool's own output. Two shapes cover nearly all of it: a
#: line a tool prefixes ("Error: ...", "usage: ..."), and a Python exception line, whose class
#: name carries the word rather than starting with it -- ``ValueError: no such column`` is the
#: line that matters and begins with a V.
_CAUSE_RE = re.compile(
    r"^([A-Za-z_][\w.]*(Error|Exception)\b|error|fatal|traceback|exception|usage:)",
    re.IGNORECASE,
)

#: How much of the cause to carry. The field is read in a campaign list and a tooltip, so it must
#: stay one glanceable line -- but a truncated cause still names the thing that went wrong, which
#: an exit code never does.
_CAUSE_CHARS = 300


def _failure_summary(message: object) -> str:
    """One line for the status field: what failed, and -- where the output says so -- why.

    Previously this was ``splitlines()[0]``, on the reasoning that a plugin may print a whole
    traceback and the status field is small. True, but it made the field useless for the most
    common failure: ``rosbags_process failed with exit code 1`` reached the campaign list, the
    web UI and the MCP status, while ``Error: unknown handler type(s): ['nav2bt_to_csv']`` --
    the line that says what to fix -- stayed in a log nobody reads until they are already stuck.

    So: the first line still leads, and the LAST cause-shaped line is appended when there is one.
    Last rather than first because a traceback ends with its exception; a tool that prints one
    error prints it once, so the two coincide.
    """
    lines = [line.strip() for line in str(message).strip().splitlines() if line.strip()]
    if not lines:
        return "failed (no output)"
    head = lines[0]
    causes = [line for line in lines[1:] if _CAUSE_RE.match(line)]
    if not causes:
        return head
    cause = causes[-1]
    if len(cause) > _CAUSE_CHARS:
        cause = cause[:_CAUSE_CHARS - 1] + "…"
    return f"{head} — {cause}"


def _batch_rosbags_commands(commands: List, skip_rosout: bool = False,
                            skip: "set | None" = None) -> List:
    """Replace all batchable rosbags_* plugin calls with rosbags_process calls.

    Groups every command whose plugin name appears in ``_ROSBAG_BATCH_MAP`` by
    their ``bag_dir`` (the subdirectory name to search for rosbags).  One
    ``rosbags_process`` command is emitted per distinct ``bag_dir``.  Each batch
    is inserted at the position of the first batchable command sharing that
    ``bag_dir``; all other batchable commands are removed.  Non-batchable
    commands keep their original order.

    The infrastructure-bag handlers (:data:`_AUTO_INFRA_HANDLERS`) are always added unless
    named in *skip* (or, for rosout, *skip_rosout*).

    Args:
        commands: Raw list of postprocessing commands from the .vast config.
        skip_rosout: When ``True``, omit rosout processing entirely (neither
            auto-injected nor taken from explicit ``rosbags_rosout_to_csv``
            commands in the config).
        skip: Batch plugin names to leave out, so an auto-injected handler can be
            declined by name without a dedicated flag per handler.

    Returns:
        New command list with batchable commands replaced by rosbags_process calls.
    """
    skip_names = set(skip or ())
    if skip_rosout:
        skip_names.add("rosbags_rosout_to_csv")
    # bag_dir → list of handler dicts for that bag dir
    bag_dir_plugins: Dict[str, List[dict]] = {}
    # bag_dir → index in result where the placeholder lives
    bag_dir_slot: Dict[str, int] = {}
    result: List = []

    for cmd in commands:
        plugin_name = cmd if isinstance(cmd, str) else list(cmd.keys())[0]
        if plugin_name in _ROSBAG_BATCH_MAP:
            handler_type, default_bag_dir = _ROSBAG_BATCH_MAP[plugin_name]
            if plugin_name in skip_names:
                continue
            params = {} if isinstance(cmd, str) else (cmd[plugin_name] or {})
            # Allow per-command bag_dir override; pop it so it's not passed to handler
            params = dict(params)
            bag_dir = params.pop("bag_dir", default_bag_dir)
            bag_dir_plugins.setdefault(bag_dir, []).append({"type": handler_type, **params})
            if bag_dir not in bag_dir_slot:
                bag_dir_slot[bag_dir] = len(result)
                result.append(None)  # reserve slot
        else:
            result.append(cmd)

    # Always include the infrastructure-bag handlers unless declined by name. A config that
    # declares one itself (with parameters) keeps its own version rather than getting a
    # second, default-configured copy.
    present = {p.get("type") for plugins in bag_dir_plugins.values() for p in plugins}
    for name in _AUTO_INFRA_HANDLERS:
        handler_type, bag_dir = _ROSBAG_BATCH_MAP[name]
        if name in skip_names or handler_type in present:
            continue
        bag_dir_plugins.setdefault(bag_dir, []).append({"type": handler_type})
        if bag_dir not in bag_dir_slot:
            bag_dir_slot[bag_dir] = len(result)
            result.append(None)

    # Fill placeholder slots with the batch commands; the auto-injected infrastructure
    # handlers run last, so an explicitly configured handler's output is in place first.
    auto_types = {_ROSBAG_BATCH_MAP[n][0] for n in _AUTO_INFRA_HANDLERS}
    for bag_dir, slot_idx in bag_dir_slot.items():
        plugins = bag_dir_plugins[bag_dir]
        auto = [p for p in plugins if p.get("type") in auto_types]
        others = [p for p in plugins if p.get("type") not in auto_types]
        result[slot_idx] = {"rosbags_process": {"plugins": others + auto, "bag_dir": bag_dir}}

    return result


def validate_postprocessing_command(command: str | dict, plugins: Dict[str, callable]) -> tuple[bool, str]:
    """Validate a postprocessing command.

    Args:
        command: Command as string (simple name) or dict (name as key with parameters)
        plugins: Dictionary of available plugins

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Parse command to get plugin name
    if isinstance(command, str):
        plugin_name = command
    elif isinstance(command, dict):
        if len(command) != 1:
            return False, f"Postprocessing command dict must have exactly one key (the plugin name), got {len(command)}"
        plugin_name = list(command.keys())[0]
    else:
        return False, f"Postprocessing command must be a string or dict, got {type(command)}"

    # Local file references (``./path.py:Class``) are resolved at run time
    # relative to the config dir, so they are valid here regardless of entry points.
    if is_file_ref(plugin_name):
        return True, ""

    if plugin_name not in plugins:
        available = ', '.join(sorted(plugins.keys()))
        return False, (
            f"Unknown postprocessing plugin: '{plugin_name}'. "
            f"Available plugins: {available if available else 'none'}. "
            f"Use 'vast results postprocess-commands' to list all plugins."
        )

    return True, ""


def get_postprocessing_commands(config_path: str) -> List[dict]:
    """Get postprocessing commands from a .vast configuration file.

    Args:
        config_path: Path to .vast configuration file

    Returns:
        List of postprocessing commands (dicts) or empty list if none defined
    """
    data_config = load_config(config_path, subsection="results_processing", allow_missing=True)
    if data_config is None:
        return []
    else:
        postprocessing_cmds = data_config.get("postprocessing", [])
        if postprocessing_cmds is None:
            return []
        else:
            return postprocessing_cmds


#: Written by a conversion that ran elsewhere -- the cluster lane's postprocessing Job --
#: and carried back with its outputs. Named here and in
#: ``cluster_execution/postprocess_job.py``; the two must agree, and this is the reading
#: half.
STAGED_PROVENANCE = "_execution/rosbags_provenance.json"


def _staged_provenance_entries(campaign_dir: str) -> List[dict]:
    """Provenance recorded by a stage that ran outside this process, or ``[]``.

    Absent is the normal case -- the local lane records everything inline -- so a missing
    file is not a problem. A malformed one is logged rather than raised: provenance is a
    description of work that already succeeded, and failing the campaign because its
    description could not be read would turn a complete result into a failed one.
    """
    path = Path(campaign_dir) / STAGED_PROVENANCE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError) as e:
        logging.getLogger(__name__).warning(
            "Could not read staged provenance %s: %s", path, e)
        return []
    entries = data.get("entries")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _write_postprocessing_provenance_yaml(
    campaign_dir: str,
    entries: List[dict],
) -> None:
    """Write postprocessing.yaml under campaign-<id>/_transient/ with all provenance entries.

    Args:
        campaign_dir: Path to the campaign-<id> directory.
        entries: List of provenance entry dicts.
    """
    transient_dir = Path(campaign_dir) / "_transient"
    try:
        transient_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    yaml_path = transient_dir / "postprocessing.yaml"

    # Paths in entries are relative to results_dir (parent of campaign_dir).
    # Rewrite them to be relative to transient_dir so the yaml is self-contained.
    results_dir_path = Path(campaign_dir).parent

    def _rel_to_transient(p: str) -> str:
        if not p:
            return p
        try:
            return str(Path(os.path.relpath(results_dir_path / p, transient_dir)))
        except (ValueError, TypeError):
            return p

    relative_entries = []
    for ent in entries:
        relative_entries.append({
            "output": _rel_to_transient(ent.get("output") or ""),
            "sources": [_rel_to_transient(s) for s in (ent.get("sources") or [])],
            "plugin": ent.get("plugin", ""),
            "params": ent.get("params") or {},
        })

    data: dict = {
        "generated_by": "robovast",
        "entries": relative_entries,
    }
    try:
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(
                data,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
    except OSError:
        pass  # skip if we cannot write



def campaign_defines_postprocessing(campaign_dir: str) -> bool:
    """True if *campaign_dir*'s snapshotted config defines postprocessing commands.

    Reads the campaign's own ``_config/*.vast`` — the authoritative config
    ``run_postprocessing`` uses — so the answer is per-campaign, not "some campaign
    under a results dir". Used to decide whether a finished campaign's stored data is
    the *postprocessed* archive or the minimal pre-postprocess data: a ``.vast`` with
    no ``results_processing.postprocessing`` entries yields the minimal data even
    though the run still reaches ``finished`` (and still builds ``data.db``).
    """
    config_dir = os.path.join(campaign_dir, "_config")
    if not os.path.isdir(config_dir):
        return False
    vasts = sorted(str(p) for p in Path(config_dir).glob("*.vast"))
    return bool(vasts) and bool(get_postprocessing_commands(vasts[0]))


def is_postprocessing_needed(
        results_dir: str,
        vast_file: Optional[str] = None,
) -> bool:
    """Check whether postprocessing needs to run for *results_dir*.

    Returns ``True`` when postprocessing commands are configured; per-rosbag
    caching inside ``rosbags_process`` handles skipping already-processed bags.

    Returns ``False`` when no postprocessing commands are configured or the
    results directory / vast file cannot be found.

    Args:
        results_dir: Directory containing run results (parent of campaign-* dirs).
        vast_file: Optional explicit path to a ``.vast`` file.

    Returns:
        ``True`` if postprocessing should be run, ``False`` otherwise.
    """
    # Same normalisation as run_postprocessing, so the two agree on which directory a
    # relative path names before one of them answers a question about the other's work.
    results_dir = os.path.abspath(results_dir)
    if not os.path.exists(results_dir):
        return False

    if vast_file is not None:
        if not os.path.isfile(vast_file):
            return False
        vast_path = os.path.abspath(vast_file)
    else:
        vast_path, _ = find_campaign_vast_file(results_dir)
        if vast_path is None:
            return False

    commands = get_postprocessing_commands(vast_path)
    return bool(commands)


def run_postprocessing(  # pylint: disable=too-many-return-statements
        results_dir: str,
        output_callback=None,
        force: bool = False,
        vast_file: Optional[str] = None,
        debug: bool = False,
        skip_rosout: bool = False,
        skip: Optional[List[str]] = None,
        skip_db: bool = False,
        skip_metadata: bool = False,
        campaign: Optional[str] = None,
):
    """Run postprocessing commands on **one campaign's** run results.

    One call processes exactly one campaign — the one named by *campaign*, or the
    most recent under *results_dir*. Each campaign snapshots its own config, so
    there is no "process every campaign with one config" mode; a caller that wants
    several loops over them and passes each one here (that way every campaign is
    processed with *its own* config).

    The postprocessing configuration is read from that campaign's
    ``_config/`` directory, unless *vast_file* is provided explicitly.

    Args:
        results_dir: Directory containing run results (parent of campaign-* dirs)
        output_callback: Optional callback function for output messages (takes message string)
        force: If True, bypass per-rosbag caches and reprocess all bags.
        vast_file: Optional explicit path to a ``.vast`` file.  When given, the
            campaign copy is ignored entirely.
        debug: If True, include full plugin stdout in output; otherwise show only the summary line.
        skip_rosout: If True, skip rosout processing entirely (shorthand for ``skip=['rosbags_rosout_to_csv']``).
        skip: List of plugin names to skip entirely (e.g. ``['rosbags_to_webm']``).
        campaign: Which campaign directory to process. ``None`` uses the most
            recent one.

    Returns:
        Tuple of (success: bool, message: str)
    """
    def output(msg):
        """Helper to call output callback or print."""
        if output_callback:
            output_callback(msg)
        else:
            print(msg)

    # Absolute from here on: container plugins are launched with cwd set to the package's
    # data/ directory (see RosbagsProcessPlugin), so a relative results_dir resolves against
    # that instead of the user's. docker_exec.sh then finds no directory to mount, skips the
    # -v silently, and the container reports 0 rosbags found as a success.
    results_dir = os.path.abspath(results_dir)

    # Validate results directory
    if not os.path.exists(results_dir):
        return False, f"Results directory does not exist: {results_dir}"

    # -- which campaign: exactly one, always ------------------------------------
    # One call postprocesses one campaign. Each campaign snapshots its own config,
    # so there is no coherent "process everything with one config" mode; a caller
    # that wants several loops and passes each campaign here.
    if campaign is None:
        _vast, _config_dir = find_campaign_vast_file(results_dir)
        if _vast is None:
            return False, (
                f"No .vast file found in any campaign-*/_config/ directory under: {results_dir}\n"
                "Ensure at least one execution campaign has been completed."
            )
        campaign = os.path.basename(str(Path(_config_dir).parent))
    campaign_dir = os.path.join(results_dir, campaign)
    if not os.path.isdir(campaign_dir):
        return False, f"Campaign {campaign!r} not found under {results_dir}"
    output(f"Campaign: {campaign}")

    # -- which config: an override wins, else the campaign's own snapshot --------
    if vast_file is not None:
        if not os.path.isfile(vast_file):
            return False, f"Override .vast file does not exist: {vast_file}"
        vast_path = os.path.abspath(vast_file)
        config_dir = os.path.dirname(vast_path)
        output(f"Using override config: {vast_path}")
    else:
        config_dir = os.path.join(campaign_dir, "_config")
        vasts = sorted(str(p) for p in Path(config_dir).glob("*.vast")) \
            if os.path.isdir(config_dir) else []
        if not vasts:
            return False, (f"No .vast file in {config_dir}. "
                           f"Is {campaign!r} a valid campaign under {results_dir}?")
        vast_path = vasts[0]
        output(f"Using config from campaign {campaign}: {vast_path}")

    # Everything below operates on this campaign only — the plugins scan its tree
    # and metadata is generated for it, never for its siblings.
    scope_dir = campaign_dir

    # Make the campaign's declared `plugins:` importable for postprocessing (entry-point
    # plugins and the deps of local file-ref plugins), installing them into the
    # campaign's own .robovast_plugins/ if absent — so a re-run in a fresh process /
    # fetched campaign (post-restart) resolves them, not just the original run.
    from robovast.common.config_plugins import ensure_plugins_importable
    ensure_plugins_importable(campaign_dir, vast_path=vast_path)

    # Read execution image from execution.yaml (if available)
    execution_image = None
    execution_yaml_path = os.path.join(campaign_dir, "_execution", "execution.yaml")
    if os.path.isfile(execution_yaml_path):
        try:
            with open(execution_yaml_path, 'r', encoding='utf-8') as f:
                exec_data = yaml.safe_load(f) or {}
            execution_image = exec_data.get("image")
            if execution_image:
                output(f"Using execution image for postprocessing: {execution_image}")
        except (yaml.YAMLError, OSError):
            pass

    # Get postprocessing commands
    commands = get_postprocessing_commands(vast_path)

    if force:
        output("Force mode: per-rosbag caches will be ignored")

    # Build unified skip set
    skip_set: set = set(skip) if skip else set()
    if skip_rosout:
        skip_set.add("rosbags_rosout_to_csv")

    # Filter out explicitly skipped plugins before batching
    if skip_set:
        filtered = []
        for cmd in commands:
            name = cmd if isinstance(cmd, str) else list(cmd.keys())[0]
            if name in skip_set:
                output(f"Skipping: {name}")
            else:
                filtered.append(cmd)
        commands = filtered

    # Load plugins
    plugins = load_postprocessing_plugins()

    # Batch all batchable rosbags_* commands into a single rosbags_process call
    # (reads each rosbag once instead of once per plugin). rosout_to_csv is always
    # included unless skipped.
    commands = _batch_rosbags_commands(commands, skip=skip_set)

    # The log merge, appended so it runs after the bag conversions it reads (rosout.csv and
    # clock_map.csv). Auto-injected for the same reason those are: a run whose output cannot
    # be read afterwards cannot be explained, and nobody should have to ask for that.
    commands = _append_auto_plugins(commands, skip_set)

    # Validate all commands first
    for command in commands:
        is_valid, error_msg = validate_postprocessing_command(command, plugins)
        if not is_valid:
            return False, error_msg

    all_provenance_entries: List[dict] = []

    with tempfile.TemporaryDirectory(prefix="robovast_provenance_") as temp_dir:
        # Execute each postprocessing command
        success = True
        # What failed, and why -- carried into the returned message rather than only printed.
        # "Postprocessing failed!" on its own sends every reader to the log to find out which of
        # five steps broke; the reason is known right here, and the status is where it is looked
        # for first (``postprocessing_error``, the campaign view's failure box).
        failures: List[str] = []

        for i, command in enumerate(commands, 1):
            # Parse command to get plugin name and parameters
            if isinstance(command, str):
                plugin_name = command
                params = {}
            elif isinstance(command, dict):
                if len(command) != 1:
                    output(f"[{i}/{len(commands)}] ✗ Invalid command format: dict must have exactly one key")
                    success = False
                    continue
                plugin_name = list(command.keys())[0]
                params = command[plugin_name] or {}
                if not isinstance(params, dict):
                    output(f"[{i}/{len(commands)}] ✗ Invalid command format: parameters must be a dict")
                    failures.append(f"{plugin_name}: parameters must be a dict")
                    success = False
                    continue
            else:
                output(f"[{i}/{len(commands)}] ✗ Invalid command format: must be string or dict, got {type(command)}")
                failures.append(f"command {i}: must be a string or a dict, got {type(command)}")
                success = False
                continue

            display_cmd = f"{plugin_name} (params: {params})" if params else plugin_name

            try:
                plugin_func = resolve_postprocessing_plugin(plugin_name, config_dir, plugins)
            except (KeyError, ValueError, ImportError, FileNotFoundError, AttributeError) as e:
                output(f"[{i}/{len(commands)}] ✗ {e}")
                failures.append(f"{plugin_name}: {e}")
                success = False
                continue

            output(f"[{i}/{len(commands)}] Executing: {display_cmd}")

            provenance_file = os.path.join(temp_dir, f"{plugin_name}_provenance.json")

            plugin_success, message, entries = execute_postprocessing_plugin(
                plugin_name=plugin_name,
                plugin_func=plugin_func,
                params=params,
                results_dir=scope_dir,
                config_dir=config_dir,
                provenance_file=provenance_file,
                execution_image=execution_image,
                debug=debug,
                force=force,
            )

            all_provenance_entries.extend(entries)

            if not plugin_success:
                output(f"✗ {message}")
                failures.append(f"{plugin_name}: {_failure_summary(message)}")
                success = False
                continue
            display_message = message if debug else message.splitlines()[0]
            output(f"✓ {display_message}")

    # Entries from work this process did not run. On the cluster lane the rosbag
    # conversions happen in a Job, and this pass runs with those steps skipped -- so
    # without merging its record, a cluster campaign's provenance would describe only
    # the steps that happened to run here, and silently omit the rest.
    all_provenance_entries = (_staged_provenance_entries(campaign_dir)
                              + all_provenance_entries)

    _record_campaign_providers(campaign_dir, output)


    # Load the campaign into the central index. This is what used to write a per-campaign
    # data.db -- a 1.1 GB SQLite file that then had to be uploaded and downloaded again on
    # the first cold query, and that could only ever answer about one campaign.
    #
    # A failure here fails postprocessing, deliberately and without a fallback. The run
    # artifacts are untouched in the object store and re-ingest is the ordinary path, so
    # nothing is lost -- the campaign is simply not queryable until postprocessing is
    # re-run. Continuing quietly would be worse: "finished" would stop meaning "queryable",
    # and the difference would surface only when somebody asked a question and got nothing
    # back. See ``common.index_db`` on why there is no degraded mode anywhere on this path.
    if skip_db:
        output("Skipping index ingest")
    else:
        from robovast.common import index_db  # pylint: disable=import-outside-toplevel
        from robovast.results_processing import \
            campaign_ingest  # pylint: disable=import-outside-toplevel

        campaign_id = os.path.basename(os.path.normpath(campaign_dir))
        with index_db.connect() as conn:
            totals = campaign_ingest.ingest_campaign(conn, campaign_dir, campaign_id)
        rows = sum(totals.values())
        output(f"✓ Indexed {campaign_id}: {rows} rows across {len(totals)} tables")

    # The provenance record is written LAST, after the ingest, and the ordering is the
    # point rather than an accident of where the call sits.
    #
    # This file is now the evidence that a campaign is postprocessed -- both for the
    # archive variant (`share_providers.naming.variant_from_record`) and for
    # `Status.postprocessed` (`common.campaign_data.campaign_has_derived_data`), which
    # used to prove it from a finished `data.db` and its absent WAL sidecars. A file has
    # no equivalent of those sidecars, so "finished" has to come from *when* it is
    # written: written before the ingest, it would claim results for a campaign whose rows
    # are not in the index -- and the ingest is the step most likely to fail, since it is
    # the one that needs the index to be up. Written after, its presence means every step
    # that produces derived data has already succeeded.
    #
    # `skip_db` is deliberately not special-cased: a caller who skipped the ingest asked
    # for a campaign that is not queryable, and the record still describes what was
    # derived, which is what an archive's recipient reads it for.
    _write_postprocessing_provenance_yaml(campaign_dir, all_provenance_entries)

    # Generate metadata.yaml in each campaign directory
    if skip_metadata:
        output("Skipping metadata generation")
    else:
        meta_success, meta_msg = generate_campaign_metadata(
            results_dir, vast_file=vast_file, output_callback=output_callback,
            campaign=campaign,
        )
        if not meta_success:
            output(f"Warning: Metadata generation failed: {meta_msg}")

    if success:
        return True, "Postprocessing completed successfully!"
    if not failures:
        # Belt and braces: a future early-exit that sets success=False without recording why
        # would otherwise regress to the message this replaced.
        return False, "Postprocessing failed (no step reported a reason; see the log)"
    detail = "; ".join(failures[:3])
    more = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
    return False, (f"Postprocessing failed: {len(failures)} of {len(commands)} step(s) — "
                   f"{detail}{more}")


def _campaign_provider_records(campaign_dir) -> list:
    """Every container's distributions record from this campaign's job dirs.

    ``_jobs/[<batch>/]job-N/`` is the shared job-artifact layout -- see
    ``run_slices.iter_run_slices`` for its authority, and ``resource_usage`` for the sibling
    that reads ``resource_usage_<container>.csv`` out of the same directories. The batch level
    is optional (the cluster lane has one, the local lane does not), so the walk is recursive
    rather than assuming either shape.

    Per CONTAINER, because that is how they were written: in the ROS shape the simulator runs
    in a container of its own, so a record from the main container alone would name none of the
    campaign's asset providers.
    """
    import glob  # pylint: disable=import-outside-toplevel

    pattern = os.path.join(str(campaign_dir), "_jobs", "**", "distributions_*.json")
    records = []
    for path in sorted(glob.glob(pattern, recursive=True)):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue          # one unreadable container's record is not the campaign's answer
        if isinstance(data, dict):
            records.append(data)
    return records


def _record_campaign_providers(campaign_dir, output) -> None:
    """Write ``_execution/providers.yaml``: which distributions supplied this campaign's assets.

    Derived here, in stage 2, rather than when the campaign was prepared. The question is
    "which installed distributions register a provider group", and only a container can answer
    it -- the packages are in its image and nowhere else. Prepared instead by walking the
    preparing process's own interpreter, the answer was right on a local lane (roqsim is
    installed beside the service) and empty on a cluster one (the service pod carries no
    simulator), so a campaign that used three private providers recorded none.

    Stage 2 is the one place both lanes run: ``run_host_postprocessing`` delegates here for the
    cluster, and the CLI and controller come here directly, "so there is no second
    implementation of the postprocessing sequence".

    Three states, and the distinction is the point. Populated is "these providers"; empty is
    "asked, and there were none"; ABSENT is "could not ask", which
    :func:`read_providers_record` documents as unknown and the publication gate classifies as
    opaque. No records, or no groups to filter by, means the question was never put -- so the
    record is left absent rather than written empty, because an empty one claims a campaign
    depended on nothing.
    """
    from robovast.common.campaign_data import (  # pylint: disable=import-outside-toplevel
        campaign_asset_groups, write_providers_record)
    from robovast.common.config_plugins import \
        providers_from_records  # pylint: disable=import-outside-toplevel

    try:
        records = _campaign_provider_records(campaign_dir)
        groups = campaign_asset_groups(campaign_dir)
        if not records or not groups:
            output(
                "Not recording asset providers: "
                + ("no container recorded its distributions (a campaign whose runs predate "
                   "that record, or never started)" if not records else
                   "this campaign's simulator backend could not be resolved here, so there is "
                   "no set of provider groups to filter by")
                + " -- leaving the record absent (unknown) rather than empty.")
            return
        providers = providers_from_records(records, groups)
        write_providers_record(campaign_dir, providers)
        output(f"✓ recorded {len(providers)} asset provider(s) from "
               f"{len(records)} container record(s)")
    except Exception as e:  # pylint: disable=broad-except
        output(f"Warning: could not record asset providers: {e}")
