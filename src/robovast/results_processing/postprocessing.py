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

"""What runs when a campaign ends: its own postprocessing steps, and its tables.

A campaign's tables are built from its records by the decoder, the first time something
names them (:mod:`robovast_data`); nothing here converts or ingests. What runs at the end is:

1. the campaign's own ``results_processing.postprocessing`` steps -- plugins by entry-point
   name or ``./path.py:Class`` -- in order, in this process; the ``rosbags_*`` entries among
   them configure the decoder and are written to its configuration record instead
   (:mod:`~robovast.results_processing.campaign_tables`);
2. the campaign-end pass: the tables the campaign declares are built for every run, its
   health checks grade it, and ``postprocessing_steps`` records how each table was made;
3. the provenance record, last, which is what says the campaign is postprocessed; then the
   campaign's metadata.
"""
import inspect
import json
import os
import re
import tempfile
from importlib.metadata import entry_points
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from robovast.common.common import load_config
from robovast.common.plugin_ref import is_file_ref, load_ref
from robovast.common.results_utils import campaign_vast_or_none, find_campaign_vast_file
from robovast.results_processing.campaign_tables import (build_tables, clear_tables,
                                                         declared_tables, is_decoder_command,
                                                         replay_tables, write_decoder_config,
                                                         write_postprocessing_steps,
                                                         write_run_health)
from robovast.results_processing.metadata import generate_campaign_metadata

POSTPROCESSING_GROUP = "robovast.postprocessing_commands"

#: What a cancelled postprocessing returns as its message, and the string a caller matches
#: to tell a cancellation from a failure.
#:
#: A cancelled run deliberately stops **before** the provenance record is written, which is
#: what makes a campaign read as postprocessed (see
#: :func:`_write_postprocessing_provenance_yaml`). So a cancelled campaign keeps every run
#: artifact it produced, is honestly reported as not postprocessed, and re-running derives
#: the rest whenever it is wanted.
POSTPROCESSING_CANCELLED = "postprocessing cancelled by stop request"


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
                                output=print, debug: bool = False, force: bool = False,
                                should_stop=None) -> Tuple[bool, List[dict]]:
    """Resolve and run a list of postprocessing commands over *results_dir*.

    Shared by ``run_postprocessing`` (the ``results_processing.postprocessing``
    path) and the campaign controller (the ``search.postprocessing`` path), so
    both load plugins identically (entry-point name or local file ref) and apply
    the same execution contract. Returns ``(success, provenance_entries)``.

    *should_stop* is read between commands and handed to each plugin that accepts it,
    exactly as in :func:`run_postprocessing`, so what a cancelled pass leaves is whole steps,
    never half of one. Decoder entries (``rosbags_*``) configure how tables are built rather
    than naming a step, and are passed over here.
    """
    plugins = load_postprocessing_plugins()
    success = True
    entries: List[dict] = []
    with tempfile.TemporaryDirectory(prefix="robovast_provenance_") as temp_dir:
        for i, command in enumerate(commands, 1):
            if should_stop is not None and should_stop():
                output(f"⏹  {POSTPROCESSING_CANCELLED}")
                return False, entries
            if is_decoder_command(command):
                continue
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
                debug=debug, force=force, should_stop=should_stop)
            entries.extend(prov)
            if not ok:
                output(f"✗ {message}")
                success = False
            else:
                output(f"✓ {message.splitlines()[0] if message else 'done'}")
    return success, entries


def _accepts(plugin_func: callable, name: str) -> bool:
    """Whether *plugin_func* declares keyword *name* (or absorbs it with ``**kwargs``).

    Plugins are user-supplied callables, so a keyword this package adds cannot simply be
    passed to all of them: one that does not declare it raises ``TypeError`` and its step
    fails with an argument error over a feature it was never asked to have.

    An unreadable signature is treated as "does not accept": builtins and C callables have
    none, and not passing an optional keyword only costs the plugin that feature.
    """
    try:
        sig = inspect.signature(plugin_func)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == name and param.kind is not inspect.Parameter.POSITIONAL_ONLY:
            return True
    return False


def execute_postprocessing_plugin(
    plugin_name: str,
    plugin_func: callable,
    params: dict,
    results_dir: str,
    config_dir: str,
    provenance_file: Optional[str] = None,
    debug: bool = False,
    force: bool = False,
    should_stop=None,
) -> Tuple[bool, str, List[dict]]:
    """Execute a postprocessing plugin with parameters.

    Args:
        plugin_name: Name of the plugin
        plugin_func: The plugin function to call
        params: Dictionary of parameters for the plugin
        results_dir: Path to the campaign-<id> directory
        config_dir: Directory containing the configuration file
        provenance_file: Optional path for a plugin to write provenance JSON to
        should_stop: Predicate a long step polls to abandon its work early. Passed only
            to plugins whose signature accepts it, so a plugin that cannot be interrupted
            -- including every third-party one written before this existed -- is called
            exactly as before rather than failing on an argument it never declared.

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
    if debug:
        kwargs['debug'] = debug
    if force:
        kwargs['force'] = force
    if should_stop is not None and _accepts(plugin_func, 'should_stop'):
        kwargs['should_stop'] = should_stop

    try:
        result = plugin_func(**kwargs)
        if isinstance(result, (list, tuple)) and len(result) >= 3:
            success, message, entries = result[0], result[1], result[2]
            return success, message, entries if isinstance(entries, list) else []
        if isinstance(result, (list, tuple)) and len(result) >= 2:
            success, message = result[0], result[1]
        else:
            success, message = result
        # Collect provenance the plugin wrote to its file, if it did
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


#: What a cause-shaped line looks like. A failing step's first line is usually its exit status
#: ("Command failed with exit code 1"), which says THAT it failed and nothing about why;
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

    The first line alone is not enough: it is usually the exit status, which reaches the
    campaign list, the web UI and the MCP status while the line that says what to fix stays in
    a log nobody reads until they are already stuck.

    So: the first line leads, and the LAST cause-shaped line is appended when there is one.
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
    # relative to the config dir, so they are valid here regardless of entry points. A
    # decoder entry configures how tables are built and names no plugin.
    if is_file_ref(plugin_name) or is_decoder_command(command):
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


def campaign_postprocessing_commands(vast_path: str, skip=None, output=None) -> List:
    """The steps a campaign's postprocessing runs, in order: its own entries, less *skip*
    and less the decoder entries, which configure how its tables are built."""
    skip_set = set(skip or ())
    kept = []
    for command in get_postprocessing_commands(vast_path):
        if is_decoder_command(command):
            continue
        name = command if isinstance(command, str) else next(iter(command))
        if name in skip_set:
            if output is not None:
                output(f"Skipping: {name}")
            continue
        kept.append(command)
    return kept


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
    though the run still reaches ``finished``.
    """
    found = campaign_vast_or_none(campaign_dir)
    return found is not None and bool(get_postprocessing_commands(str(found)))


def is_postprocessing_needed(
        results_dir: str,
        vast_file: Optional[str] = None,
) -> bool:
    """Check whether postprocessing needs to run for *results_dir*.

    Returns ``True`` when postprocessing commands are configured.

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


def run_postprocessing(  # pylint: disable=too-many-return-statements,too-many-branches
        results_dir: str,
        output_callback=None,
        force: bool = False,
        vast_file: Optional[str] = None,
        debug: bool = False,
        skip: Optional[List[str]] = None,
        skip_metadata: bool = False,
        campaign: Optional[str] = None,
        should_stop=None,
        replay: bool = False,
):
    """Run what ends **one campaign**: its steps, its tables, its provenance, its metadata.

    One call processes exactly one campaign -- the one named by *campaign*, or the most
    recent under *results_dir* -- with that campaign's own ``_config/`` snapshot, unless
    *vast_file* names another.

    Args:
        results_dir: Directory containing campaigns.
        output_callback: Called with each progress line.
        force: Clear the campaign's built tables first, so everything declared is built again
            -- by this decoder, from the records.
        vast_file: An explicit ``.vast`` to read instead of the campaign's own.
        debug: Pass each plugin's full output through rather than its summary line.
        skip: Step names to leave out.
        skip_metadata: Leave out the metadata record.
        campaign: Which campaign directory to process; ``None`` is the most recent.
        should_stop: Polled between steps, and handed to the steps that can honour it
            mid-flight, so a stopped campaign stops here in seconds. A cancelled pass never
            writes the provenance record, so it never reads as postprocessed.
        replay: Clear the campaign's tables and build every table its records can give, for
            every run -- not only the declared ones -- before the campaign-end pass. A
            replay yields the rows a live watcher wrote as the runs went.

    Returns:
        ``(success, message)``. A cancelled run returns ``(False, POSTPROCESSING_CANCELLED)``.
    """
    def output(msg):
        if output_callback:
            output_callback(msg)
        else:
            print(msg)

    results_dir = os.path.abspath(results_dir)
    if not os.path.exists(results_dir):
        return False, f"Results directory does not exist: {results_dir}"

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

    if vast_file is not None:
        if not os.path.isfile(vast_file):
            return False, f"Override .vast file does not exist: {vast_file}"
        vast_path = os.path.abspath(vast_file)
        config_dir = os.path.dirname(vast_path)
        output(f"Using override config: {vast_path}")
    else:
        config_dir = os.path.join(campaign_dir, "_config")
        found = campaign_vast_or_none(campaign_dir)
        if found is None:
            # Absent and empty are different faults: a campaign whose results live elsewhere
            # has no `_config/` here at all.
            return False, (
                f"No frozen config in {config_dir}, so {campaign!r}'s configuration cannot "
                f"be read." + ("" if os.path.isdir(config_dir) else
                               f" That directory does not exist: the campaign's results were "
                               f"never projected into {results_dir}."))
        vast_path = str(found)
        output(f"Using config from campaign {campaign}: {vast_path}")

    # The campaign's declared `plugins:` importable here, installed into its own
    # .robovast_plugins/ if absent, so a re-run in a fresh process resolves them too.
    from robovast.common.config_plugins import \
        ensure_plugins_importable  # pylint: disable=import-outside-toplevel
    ensure_plugins_importable(campaign_dir, vast_path=vast_path)

    # The decoder builds this campaign's tables with its own configuration, which an edited
    # postprocessing block may just have changed.
    write_decoder_config(campaign_dir, vast_path)
    if force or replay:
        freed = clear_tables(campaign_dir)
        output(f"{'Replay' if replay else 'Force mode'}: cleared the campaign's tables "
               f"({freed // (1024 * 1024)} MiB); "
               + ("every table the records can give is built again" if replay
                  else "what is declared is built again"))

    plugins = load_postprocessing_plugins()
    commands = campaign_postprocessing_commands(vast_path, skip=skip, output=output)
    for command in commands:
        is_valid, error_msg = validate_postprocessing_command(command, plugins)
        if not is_valid:
            return False, error_msg

    success = True
    # What failed, and why -- carried into the returned message, which is where the status
    # (``postprocessing_error``, the campaign view's failure box) is read from.
    failures: List[str] = []
    all_provenance_entries: List[dict] = []
    with tempfile.TemporaryDirectory(prefix="robovast_provenance_") as temp_dir:
        for i, command in enumerate(commands, 1):
            if should_stop is not None and should_stop():
                output(f"⏹  {POSTPROCESSING_CANCELLED}")
                return False, POSTPROCESSING_CANCELLED
            plugin_name = command if isinstance(command, str) else list(command.keys())[0]
            params = {} if isinstance(command, str) else (command[plugin_name] or {})
            if isinstance(command, dict) and (len(command) != 1 or not isinstance(params, dict)):
                output(f"[{i}/{len(commands)}] ✗ Invalid command format: {command!r}")
                failures.append(f"command {i}: a name, or a one-key mapping to parameters")
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
            plugin_success, message, entries = execute_postprocessing_plugin(
                plugin_name=plugin_name, plugin_func=plugin_func, params=params,
                results_dir=campaign_dir, config_dir=config_dir,
                provenance_file=os.path.join(temp_dir, f"{plugin_name}_provenance.json"),
                debug=debug, force=force, should_stop=should_stop)
            all_provenance_entries.extend(entries)
            if not plugin_success:
                output(f"✗ {message}")
                failures.append(f"{plugin_name}: {_failure_summary(message)}")
                success = False
                continue
            output(f"✓ {message if debug else message.splitlines()[0]}")

    if should_stop is not None and should_stop():
        output(f"⏹  {POSTPROCESSING_CANCELLED}")
        return False, POSTPROCESSING_CANCELLED

    _record_campaign_providers(campaign_dir, output)

    # The campaign-end pass. Its failures fail postprocessing, without a fallback: the records
    # are untouched and re-running builds again, while continuing quietly would let
    # "postprocessed" stop meaning "what it declares is there".
    tables = declared_tables(vast_path)

    def _progress(done, total):
        if done == total or done % 25 == 0:
            output(f"  built {done}/{total} run(s)")

    problems = []
    if replay:
        output("Replaying every table the records can give, for every run")
        problems = replay_tables(campaign_dir, progress=_progress)
    output(f"Building {len(tables)} declared table(s): {', '.join(tables)}")
    problems += [p for p in build_tables(campaign_dir, tables, progress=_progress)
                 if p not in problems]
    if problems:
        shown = "; ".join(str(p) for p in problems[:3])
        more = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
        output(f"✗ {len(problems)} table(s) could not be built for some runs: {shown}{more}")
        failures.append(f"tables: {shown}{more}")
        success = False
    else:
        output("✓ declared tables built")
    if should_stop is not None and should_stop():
        output(f"⏹  {POSTPROCESSING_CANCELLED}")
        return False, POSTPROCESSING_CANCELLED
    graded = write_run_health(campaign_dir, vast_path)
    output(f"✓ run_health: {graded} row(s)")
    write_postprocessing_steps(campaign_dir, all_provenance_entries)

    # The provenance record is written LAST among the derived data: it is the evidence that a
    # campaign is postprocessed, both for the archive variant and for `Status.postprocessed`,
    # and a file has no way to say "finished" but when it is written. Written before the
    # metadata step, and still written when that fails: it records what was derived, which is
    # true either way -- the failure is carried by the return below.
    _write_postprocessing_provenance_yaml(campaign_dir, all_provenance_entries)

    meta_failure = ""
    if skip_metadata:
        output("Skipping metadata generation")
    else:
        meta_success, meta_msg = generate_campaign_metadata(
            results_dir, vast_file=vast_file, output_callback=output_callback,
            campaign=campaign,
        )
        if not meta_success:
            # A failure, not a warning: what goes unwritten is the campaign's FAIR provenance
            # record, and a warning would let it export and be shared as complete.
            meta_failure = meta_msg
            output(f"Metadata generation failed: {meta_msg}")

    if success and not meta_failure:
        return True, "Postprocessing completed successfully!"

    reasons = []
    if failures:
        detail = "; ".join(failures[:3])
        more = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
        reasons.append(f"{len(failures)} step(s) — {detail}{more}")
    if meta_failure:
        reasons.append(
            f"the campaign has no FAIR provenance record: {meta_failure}. Its tables are "
            "complete and queryable; re-running postprocessing writes the record")
    return False, "Postprocessing failed: " + " | ".join(reasons)


def _campaign_provider_records(campaign_dir) -> list:
    """Every container's distributions record from this campaign's job dirs.

    ``_jobs/[<batch>/]job-N/`` is the shared job-artifact layout -- see
    :mod:`robovast_decode.run_slices`, and :mod:`robovast_decode.resource_usage` for the
    sibling that reads ``resource_usage_<container>.csv`` out of the same directories. The batch level
    is optional (an unpacked campaign has none), so the walk is recursive rather than
    assuming either shape.

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

    Derived here, at the end of the campaign, rather than when the campaign was prepared. The question is
    "which installed distributions register a provider group", and only a container can answer
    it -- the packages are in its image and nowhere else. Prepared instead by walking the
    preparing process's own interpreter, the answer was right on a machine with roqsim
    installed beside the service and empty in the service pod, which carries no simulator,
    so a campaign that used three private providers recorded none.

    Postprocessing is the one place every runner runs, so there is no second implementation
    of the sequence.

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
