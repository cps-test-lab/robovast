#!/usr/bin/env python3
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

"""CLI plugin for run definition."""

import fnmatch
import os
import sys
from importlib.metadata import entry_points

import click
import yaml

from robovast.client.errors import handle_cli_exception
from robovast.common import (convert_dataclasses_to_dict, filter_configs,
                             generate_scenario_variations, get_scenario_parameters,
                             prepare_campaign_configs)


@click.group()
def configuration():
    """Manage run configuration.
    """


@configuration.command(name='list')
@click.argument('vast', type=click.Path(exists=True, dir_okay=False), metavar='VAST')
@click.option('--debug', is_flag=True, help='Show internal values starting with _')
def list_cmd(vast, debug):
    """List the configurations VAST expands to, without generating files.

    Shows what would be generated, creating none of it.
    """
    config = vast

    try:
        campaign_data = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=None,
            output_dir=None
        )
        configs_data = campaign_data["configs"]
        configs = convert_dataclasses_to_dict(configs_data)
        if configs:
            # Filter out internal values unless --debug is enabled
            if debug:
                filtered_documents = configs
            else:
                filtered_documents = filter_configs(configs)

            # Build output string with document separators
            output_parts = []
            for i, doc in enumerate(filtered_documents):
                if i > 0:
                    output_parts.append("---")
                output_parts.append(yaml.dump(doc, default_flow_style=False, sort_keys=False).rstrip())

            output = "\n".join(output_parts)
            click.echo(output)
    except Exception as e:
        handle_cli_exception(e)


@configuration.command(name='info')
@click.argument('vast', type=click.Path(exists=True, dir_okay=False), metavar='VAST')
def info(vast):
    """Show how many configurations and runs VAST expands to."""
    config = vast

    try:
        campaign_data = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=None,
            output_dir=None
        )
        configs = campaign_data["configs"]
        runs_per_config = campaign_data.get("execution", {}).get("runs", 1)
        total_runs = len(configs) * runs_per_config

        click.echo("Configuration Overview")
        click.echo("======================")
        click.echo(f"Configurations: {len(configs)}")
        click.echo(f"Runs per configuration: {runs_per_config}")
        click.echo(f"Total runs: {total_runs}")
        click.echo(f"Scenario file: {campaign_data.get('scenario_file', 'N/A')}")
        click.echo(f"VAST file: {campaign_data.get('vast', 'N/A')}")
        if "metadata" in campaign_data:
            click.echo(f"Metadata: {campaign_data['metadata']}")
    except Exception as e:
        handle_cli_exception(e)


@configuration.command(name='validate')
@click.argument('vast', type=click.Path(exists=True, dir_okay=False), metavar='VAST')
def validate(vast):
    """Validate VAST, reporting ALL problems at once.

    Runs the same collect-all linter as the ``validate_project`` MCP tool: YAML,
    schema, the scenario file and its parameter references, and every plugin
    reference — variation types, postprocessing commands, and the search
    strategy/extractor — whether installed or local ``./path.py:Class`` refs.
    Exits non-zero if any problem is found.
    """
    from robovast.common.config_validation import \
        validate_project_file  # pylint: disable=import-outside-toplevel

    report = validate_project_file(vast)

    problems = report.get("problems", [])
    if report.get("valid"):
        click.echo(click.style("✓ Valid", fg="green"))
        click.echo(f"Configurations: {report.get('configs', 0)}")
        click.echo(f"Runs per configuration: {report.get('runs_per_config', 0)}")
        click.echo(f"Total runs: {report.get('total_trials', 0)}")
        return

    click.echo(click.style(f"✗ {len(problems)} problem(s) found:", fg="red"))
    for p in problems:
        location = " ".join(filter(None, [
            f"[{p['stage']}]" if p.get("stage") else None,
            f"config={p['config']}" if p.get("config") else None,
            f"field={p['field']}" if p.get("field") else None,
        ]))
        click.echo(f"  - {location}: {p.get('message', '')}" if location
                   else f"  - {p.get('message', '')}")
    sys.exit(1)


@configuration.command(name='upgrade')
@click.argument('vast', type=click.Path(exists=True, dir_okay=False), metavar='VAST')
@click.option('--dry-run', is_flag=True,
              help='Report what would change without writing the file.')
def upgrade(vast, dry_run):
    """Bring VAST to the current config version, in place.

    Only for a file you are AUTHORING. An archived campaign under ``<campaign>/_config/``
    is migrated automatically when read and is deliberately never rewritten -- it is the
    record of what its author wrote.

    Comments are preserved. Exits non-zero when the file cannot be brought forward, which
    happens when it uses something the current version cannot express; the message names
    what.
    """
    from robovast.common.migrations import (  # pylint: disable=import-outside-toplevel
        SUPPORTED_CONFIG_VERSION, ConfigVersionError, config_version, upgrade_config_file)

    config = vast
    try:
        with open(config, 'r', encoding='utf-8') as handle:
            before = (list(yaml.safe_load_all(handle)) or [{}])[0] or {}
        was = config_version(before)
        _, applied = upgrade_config_file(config, write=not dry_run)
    except ConfigVersionError as e:
        click.echo(click.style(f"✗ {e}", fg="red"))
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - reported, not raised, like every verb here
        handle_cli_exception(e)
        return

    if not applied:
        click.echo(f"Already at version {SUPPORTED_CONFIG_VERSION}: {config}")
        return
    verb = "would apply" if dry_run else "applied"
    click.echo(click.style(
        f"✓ {verb} {', '.join(applied)} ({was} -> {SUPPORTED_CONFIG_VERSION}): {config}",
        fg="green"))
    if dry_run:
        click.echo("Nothing written (--dry-run).")


@configuration.command(name='plugins')
@click.option('--group', default='robovast.variation_types', show_default=True,
              help='Entry-point group to list.')
def plugins_cmd(group):
    """List installed plugins for an extension group.

    Defaults to the variation types usable in a ``.vast`` ``variations`` block.
    Pass a name to ``vast configuration plugin-info`` for its parameter schema.
    """
    eps = sorted(entry_points(group=group), key=lambda e: e.name)
    if not eps:
        click.echo(click.style(f"No plugins found in group '{group}'.", fg="yellow"))
        return
    width = max(len(ep.name) for ep in eps)
    for ep in eps:
        summary = _plugin_doc_summary(ep) or ""
        click.echo(f"  {ep.name.ljust(width)}  {summary}")


@configuration.command(name='plugin-info')
@click.argument('name', metavar='NAME')
@click.option('--group', default='robovast.variation_types', show_default=True,
              help='Entry-point group the plugin belongs to.')
def plugin_info(name, group):
    """Show a plugin's docstring and its accepted parameter schema.

    The CLI counterpart to the ``get_plugin_details`` MCP tool: for a plugin that
    declares a parameter model (variation types via ``CONFIG_CLASS``, search
    strategies via ``PARAMS_MODEL``) it prints each parameter's name, type,
    whether it is required, and its default — the field schema that the top-level
    ``.vast`` JSON Schema cannot express. Exits non-zero for an unknown plugin.
    """
    from robovast.common.plugin_schema import \
        plugin_parameter_schema  # pylint: disable=import-outside-toplevel

    matches = [ep for ep in entry_points(group=group) if ep.name == name]
    if not matches:
        click.echo(click.style(
            f"✗ No plugin '{name}' found in group '{group}'.", fg="red"))
        sys.exit(1)

    ep = matches[0]
    click.echo(click.style(ep.name, fg="green", bold=True) + f"  ({ep.value})")
    doc = _plugin_doc_summary(ep, max_lines=0)
    if doc:
        click.echo(doc)

    params = plugin_parameter_schema(group, name)
    if not params:
        click.echo("\nParameters: (none declared)")
        return

    click.echo("\nParameters:")
    headers = ("name", "type", "required", "default")
    rows = [(p["name"], p["type"], "yes" if p["required"] else "no",
             "" if "default" not in p else repr(p["default"])) for p in params]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    click.echo("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    click.echo("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        click.echo("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _plugin_doc_summary(ep, max_lines: int = 1):
    """Return up to *max_lines* non-blank docstring lines for entry point *ep*."""
    import textwrap  # pylint: disable=import-outside-toplevel
    try:
        obj = ep.load()
    except Exception:  # noqa: BLE001 - a broken plugin must not break the listing
        return None
    raw = getattr(obj, "__doc__", None) or ""
    lines = [l for l in textwrap.dedent(raw).strip().splitlines() if l.strip()]
    if not lines:
        return None
    selected = lines if max_lines == 0 else lines[:max_lines]
    return "\n".join(selected)


@configuration.command(name='export-configs')
@click.argument('args', nargs=-1, required=True, metavar='PATTERN... OUTPUT')
@click.option('--vast', 'input_file', required=True,
              type=click.Path(exists=True, dir_okay=False), metavar='VAST',
              help='The .vast to export configurations from. An option rather than a '
                   'positional because PATTERN... OUTPUT is already variadic, and a '
                   'third positional could not be told from a pattern.')
@click.option('--remove', is_flag=True, default=False,
              help='Remove the exported configurations from the source .vast file.')
def export_configs(args, input_file, remove):
    """Export configurations matching PATTERN(s) into a new .vast file.

    PATTERN is one or more glob patterns (e.g. 'unirandom*') matched against
    configuration names. OUTPUT is the last argument and specifies the path
    of the new .vast file to create.

    Example::

    \b
        vast config export-configs --vast mine.vast unirandom* new.vast
        vast config export-configs --vast mine.vast --remove 'office*' subset.vast
    """
    if len(args) < 2:
        raise click.UsageError(
            "Requires at least one PATTERN and an OUTPUT file.\n"
            "Usage: vast config export-configs PATTERN... OUTPUT"
        )

    patterns = args[:-1]
    output = args[-1]

    source_path = input_file

    try:
        with open(source_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except Exception as e:
        handle_cli_exception(e)
        return

    configurations = data.get('configuration', [])
    if not isinstance(configurations, list):
        click.echo("Error: 'configuration' key is missing or not a list.", err=True)
        sys.exit(1)

    def matches_any(name):
        return any(fnmatch.fnmatch(name, p) for p in patterns)

    matched = [cfg for cfg in configurations if matches_any(cfg.get('name', ''))]

    if not matched:
        click.echo(
            f"No configurations matched patterns: {', '.join(patterns)}", err=True
        )
        sys.exit(1)

    data['configuration'] = matched

    output_dir = os.path.dirname(output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    if os.path.exists(output):
        if not click.confirm(f"File '{output}' already exists. Overwrite?", default=True):
            click.echo("Aborted.")
            sys.exit(0)

    try:
        with open(output, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception as e:
        handle_cli_exception(e)
        return

    click.echo(
        f"✓ Exported {len(matched)} configuration(s) matching "
        f"{', '.join(repr(p) for p in patterns)} → {output}"
    )

    if remove:
        remaining = [cfg for cfg in configurations if not matches_any(cfg.get('name', ''))]
        source_data = dict(data)
        source_data['configuration'] = remaining
        try:
            with open(source_path, 'w', encoding='utf-8') as f:
                yaml.dump(source_data, f, default_flow_style=False,
                          sort_keys=False, allow_unicode=True)
        except Exception as e:
            handle_cli_exception(e)
            return
        click.echo(
            f"✓ Removed {len(matched)} configuration(s) from {source_path}."
        )


@configuration.command(name='import-configs')
@click.argument('vast', type=click.Path(exists=True, dir_okay=False), metavar='VAST')
@click.argument('source', type=click.Path(exists=True), metavar='SOURCE')
def import_configs(vast, source):
    """Import configurations from SOURCE into VAST.

    Reads all ``configuration`` entries from SOURCE, validates that they produce valid
    scenario configurations (via a dry-run expansion), and appends the passing entries
    to VAST. Configs whose names already exist there are skipped.

    Example::

        vast config import-configs mine.vast other.vast
    """
    import tempfile  # pylint: disable=import-outside-toplevel

    dest_path = vast

    # Load source file
    try:
        with open(source, 'r', encoding='utf-8') as f:
            source_data = yaml.safe_load(f)
    except Exception as e:
        handle_cli_exception(e)
        return

    incoming = source_data.get('configuration', [])
    if not isinstance(incoming, list) or not incoming:
        click.echo("Error: No 'configuration' entries found in source file.", err=True)
        sys.exit(1)

    # Load destination file
    try:
        with open(dest_path, 'r', encoding='utf-8') as f:
            dest_data = yaml.safe_load(f)
    except Exception as e:
        handle_cli_exception(e)
        return

    existing_names = {cfg.get('name') for cfg in dest_data.get('configuration', [])}

    # Raise error if any incoming config names already exist
    duplicates = [cfg.get('name') for cfg in incoming if cfg.get('name') in existing_names]
    if duplicates:
        click.echo(
            f"Error: The following configuration name(s) already exist in {dest_path}:\n"
            + "\n".join(f"  - {n}" for n in duplicates),
            err=True,
        )
        sys.exit(1)

    new_configs = incoming

    # Validate new configs by doing a dry-run expansion in a temp file
    click.echo(f"Validating {len(new_configs)} new configuration(s)...")

    probe_data = dict(dest_data)
    probe_data['configuration'] = new_configs

    valid_configs = []
    invalid_configs = []

    for cfg in new_configs:
        probe_single = dict(dest_data)
        probe_single['configuration'] = [cfg]
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.vast', delete=False, encoding='utf-8'
            ) as tmp:
                yaml.dump(probe_single, tmp, default_flow_style=False,
                          sort_keys=False, allow_unicode=True)
                tmp_path = tmp.name

            campaign_data = generate_scenario_variations(
                variation_file=tmp_path,
                progress_update_callback=None,
                output_dir=None,
            )
            if campaign_data.get('configs'):
                valid_configs.append(cfg)
                click.echo(f"  ✓ {cfg.get('name')}")
            else:
                invalid_configs.append(cfg.get('name'))
                click.echo(f"  ✗ {cfg.get('name')} (produced no configs)", err=True)
        except Exception as exc:
            invalid_configs.append(cfg.get('name'))
            click.echo(f"  ✗ {cfg.get('name')}: {exc}", err=True)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    if not valid_configs:
        click.echo("No valid configurations to import.", err=True)
        sys.exit(1)

    # Append valid configs to destination file
    dest_data.setdefault('configuration', []).extend(valid_configs)

    try:
        with open(dest_path, 'w', encoding='utf-8') as f:
            yaml.dump(dest_data, f, default_flow_style=False,
                      sort_keys=False, allow_unicode=True)
    except Exception as e:
        handle_cli_exception(e)
        return

    click.echo(
        f"\n✓ Imported {len(valid_configs)} configuration(s) into {dest_path}."
    )
    if invalid_configs:
        click.echo(
            f"  {len(invalid_configs)} configuration(s) were skipped due to errors: "
            f"{', '.join(invalid_configs)}",
            err=True,
        )


@configuration.command()
@click.argument('vast', type=click.Path(exists=True, dir_okay=False), metavar='VAST')
@click.argument('output-dir', type=click.Path())
@click.option('--keep-transient', is_flag=True, default=False,
              help='Keep and display temporary folders used during generation (e.g. by FloorplanGeneration).')
@click.option('--no-cache', is_flag=True, default=False,
              help='Skip cache lookup and force a fresh generation even if inputs are unchanged.')
def generate(vast, output_dir, keep_transient, no_cache):
    """Generate VAST's run configurations and output files into OUTPUT_DIR."""
    config = vast

    click.echo(f"Generating scenario configurations...")

    try:
        os.makedirs(output_dir, exist_ok=True)

        campaign_data = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=None,
            output_dir=output_dir,
            use_cache=not no_cache,
        )
        configs = campaign_data["configs"]

        if configs:
            config_path_result = os.path.join(output_dir, "out_template")
            prepare_campaign_configs(config_path_result, campaign_data)
            click.echo(f"✓ Successfully generated {len(configs)} scenario configurations in directory '{output_dir}'.")

            if keep_transient:
                _print_transient_locations(campaign_data)
        else:
            click.echo("✗ Failed to generate scenario configurations", err=True)
            sys.exit(1)

    except Exception as e:
        if keep_transient:
            _print_transient_dirs_from_output(output_dir)
        handle_cli_exception(e)


def _print_transient_locations(campaign_data):
    """Print all transient directories produced during config generation."""
    transient_dirs = set()
    _gen_output_dir = campaign_data.get("_output_dir", "")

    for config in campaign_data.get("configs", []):
        for _rel, path in config.get("_config_transient_files", []):
            abs_path = path if os.path.isabs(path) else os.path.join(_gen_output_dir, path)
            transient_dirs.add(os.path.dirname(abs_path))

    for _rel, abs_path in campaign_data.get("_transient_files", []):
        transient_dirs.add(os.path.dirname(abs_path))

    if transient_dirs:
        click.echo("\nTransient directories (--keep-transient):")
        for d in sorted(transient_dirs):
            click.echo(f"  {d}")
    else:
        click.echo("\nNo transient directories were produced.")


def _print_transient_dirs_from_output(output_dir):
    """Scan output_dir for transient working directories left by variations."""
    if not os.path.isdir(output_dir):
        return
    transient_dirs = []
    for entry in sorted(os.scandir(output_dir), key=lambda e: e.name):
        if entry.is_dir() and entry.name != "out_template":
            transient_dirs.append(entry.path)
    if transient_dirs:
        click.echo("\nTransient directories (--keep-transient):", err=True)
        for d in transient_dirs:
            click.echo(f"  {d}", err=True)


@configuration.command(name='variation-types')
def variation_types():
    """List available variation types.

    Shows all registered variation type entry points that can be used
    in the variations section of .vast configuration files.
    """
    click.echo("Available variation types:")
    click.echo("")

    try:
        eps = entry_points()
        variation_eps = eps.select(group='robovast.variation_types')

        if not variation_eps:
            click.echo("No variation types found.", err=True)
            sys.exit(1)

        for ep in variation_eps:
            try:
                # Load the class to verify it's accessible
                variation_class = ep.load()
                click.echo(f"- {ep.name}")
                # Try to get docstring if available
                if variation_class.__doc__:
                    doc_lines = variation_class.__doc__.strip().split('\n')
                    if doc_lines:
                        click.echo(f"  {doc_lines[0].strip()}")
                click.echo()
            except Exception as e:
                click.echo(f"  {ep.name} (Failed to load: {e})", err=True)
                click.echo()

    except Exception as e:
        handle_cli_exception(e)


@configuration.command(name='variation-points')
@click.argument('vast', type=click.Path(exists=True, dir_okay=False), metavar='VAST')
def variation_points(vast):
    """List the variation points VAST's scenarios offer.

    Shows every scenario parameter that can be varied.
    """
    config = vast

    click.echo("Loading scenario parameter template...")
    click.echo("")

    try:
        campaign_data = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=None,
            output_dir=None,
        )
        configs = campaign_data["configs"]
    except Exception as e:
        handle_cli_exception(e)

    unique_scenarios = set()
    for config in configs:
        unique_scenarios.add(config.get('_scenario_file'))

    for scenario_file in unique_scenarios:
        if not scenario_file:
            click.echo("Error: No scenario file found in configuration", err=True)
            sys.exit(1)

        # Make scenario path absolute relative to config file
        if not os.path.isabs(scenario_file):
            scenario_file = os.path.join(os.path.dirname(config), scenario_file)

        if not os.path.exists(scenario_file):
            click.echo(f"Error: Scenario file does not exist: {scenario_file}", err=True)
            sys.exit(1)

        # Get the scenario parameter template
        scenario_template = get_scenario_parameters(scenario_file)

        if scenario_template:
            scenario_parameters = next(iter(scenario_template.values()))
        else:
            scenario_parameters = None

        if not scenario_parameters:
            click.echo("No variation points found in scenario", err=True)
            sys.exit(1)

        # Display the parameters in a readable format
        print(f"Variation points in scenario file: {scenario_file}")
        for param in scenario_parameters:
            click.echo(f"    {param["name"]}: {param["type"] if not param["is_list"] else f'list[{param["type"]}]'}")
