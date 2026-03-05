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

"""CLI for results processing and management."""

import sys

import click

from robovast.evaluation.merge_results import merge_results
from robovast.common.cli import get_project_config, handle_cli_exception
from robovast.common.cli.project_config import ProjectConfig
from robovast.results_processing import run_postprocessing
from robovast.results_processing.postprocessing import \
    load_postprocessing_plugins
from robovast.results_processing.publication import \
    run_publication, load_publication_plugins


@click.group()
def results():
    """Manage run results.

    Tools for postprocessing scenario execution results,
    including data conversion, merging, and metadata generation.
    """


@results.command(name='postprocess')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing run results (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force postprocessing even if results directory is unchanged (bypasses caching)')
@click.option('--override', '-o', default=None, metavar='VAST_FILE',
              help='Override the .vast file used for postprocessing instead of the one '
                   'found in campaign-<id>/_config/')
def postprocess_cmd(results_dir, force, override):
    """Run postprocessing commands on run results.

    Executes postprocessing commands defined in the .vast file found in the
    most recent ``campaign-<id>/_config/`` directory of the results directory.
    Postprocessing is skipped if the result-directory is unchanged,
    unless --force is specified.

    Use --override to supply a .vast file explicitly instead of the campaign copy.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    # Resolve results_dir from project config when not explicitly provided.
    # postprocess never uses config_path from the project file (it always reads
    # the .vast from campaign-<id>/_config/ or --override), so only results_dir
    # is needed and config_path validation is intentionally skipped.
    if results_dir is None:
        raw_config = ProjectConfig.load()
        if not raw_config or not raw_config.results_dir:
            raise click.ClickException(
                "Project not initialized. Run 'vast init <config-file>' first."
            )
        results_dir = raw_config.results_dir

    click.echo("Starting postprocessing...")
    click.echo(f"Results directory: {results_dir}")
    if override:
        click.echo(f"Override .vast file: {override}")
    if force:
        click.echo("Force mode enabled: bypassing cache")
    click.echo("-" * 60)

    # Run postprocessing
    success, message = run_postprocessing(
        results_dir=results_dir,
        output_callback=click.echo,
        force=force,
        vast_file=override,
    )

    click.echo("\n" + "=" * 60)
    if not success:
        click.echo(f"\u2717 {message}", err=True)
        sys.exit(1)


@results.command(name='publish')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing run results (uses project results dir if not specified)')
@click.option('--override', '-o', default=None, metavar='VAST_FILE',
              help='Override the .vast file used for publication instead of the one '
                   'found in campaign-<id>/_config/')
@click.option('--force', '-f', is_flag=True,
              help='Overwrite existing output files without prompting.')
@click.option('--skip-postprocessing', is_flag=True,
              help='Skip postprocessing and only run publication plugins.')
def publish_cmd(results_dir, override, force, skip_postprocessing):
    """Publish run results using configured publication plugins.

    Executes postprocessing plugins (unless ``--skip-postprocessing`` is used)
    followed by publication plugins defined in the .vast file found in the
    most recent ``campaign-<id>/_config/`` directory of the results directory.
    Publication plugins handle packaging and distribution of results.

    Use --override to supply a .vast file explicitly instead of the campaign copy.
    Use --force to overwrite existing output files without prompting.
    Use --skip-postprocessing to only run publication without postprocessing.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    # Resolve results_dir from project config when not explicitly provided.
    if results_dir is None:
        raw_config = ProjectConfig.load()
        if not raw_config or not raw_config.results_dir:
            raise click.ClickException(
                "Project not initialized. Run 'vast init <config-file>' first."
            )
        results_dir = raw_config.results_dir

    click.echo("Starting publication...")
    click.echo(f"Results directory: {results_dir}")
    if override:
        click.echo(f"Override .vast file: {override}")
    click.echo("-" * 60)

    # Run postprocessing first (unless skipped)
    if not skip_postprocessing:
        click.echo("Running postprocessing...")
        pp_success, pp_message = run_postprocessing(
            results_dir=results_dir,
            output_callback=click.echo,
            vast_file=override,
        )
        click.echo()
        if not pp_success:
            click.echo("\n" + "=" * 60)
            click.echo(f"✗ Postprocessing failed: {pp_message}")
            click.echo("=" * 60)
            sys.exit(1)

    # Run publication
    success, message = run_publication(
        results_dir=results_dir,
        output_callback=click.echo,
        vast_file=override,
        force=force,
    )

    click.echo("\n" + "=" * 60)
    if not success:
        click.echo(f"\u2717 {message}", err=True)
        sys.exit(1)
    click.echo(f"\u2713 {message}")


@results.command(name='merge-campaigns')
@click.argument('merged_campaign_dir', type=click.Path())
@click.option('--results-dir', '-r', default=None,
              help='Source directory containing run-\\* directories (uses project results directory if not specified)')
def merge_results_cmd(merged_campaign_dir, results_dir):
    """Merge campaign directories with identical configs into one merged_campaign_dir.

    Groups campaign-directory/config-directory by config_identifier from config.yaml.
    Run folders (0, 1, 2, ...) from all campaigns are renumbered and copied.
    Original campaign directories are not modified.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    if results_dir is not None:
        source_dir = results_dir
    else:
        project_config = get_project_config()
        source_dir = project_config.results_dir

    click.echo(f"Merging from {source_dir} into {merged_campaign_dir}...")
    try:
        success, message = merge_results(source_dir, merged_campaign_dir)
        if success:
            click.echo(f"\u2713 {message}")
        else:
            click.echo(f"\u2717 {message}", err=True)
            sys.exit(1)
    except Exception as e:
        handle_cli_exception(e)


@results.command(name='postprocess-commands')
def list_postprocessing_commands():
    """List all available postprocessing command plugins.

    Shows plugin names that can be used in the ``results_processing.postprocessing`` section
    of the configuration file, along with their descriptions and parameters.
    """
    plugins = load_postprocessing_plugins()

    if not plugins:
        click.echo("No postprocessing command plugins available.")
        click.echo("\nPostprocessing commands can be registered as plugins.")
        click.echo("See documentation for how to add custom postprocessing commands.")
        return

    click.echo("Available postprocessing command plugins:")
    click.echo("=" * 70)

    # Sort by plugin name for consistent output
    for plugin_name in sorted(plugins.keys()):
        click.echo(f"\n{plugin_name}")

        # Try to get the function's docstring
        try:
            func = plugins[plugin_name]
            if func.__doc__:
                # Clean up docstring and display first line
                doc_lines = [line.strip() for line in func.__doc__.strip().split('\n') if line.strip()]
                if doc_lines:
                    click.echo(f"  Description: {doc_lines[0]}")
        except Exception:
            pass

    click.echo("\n" + "=" * 70)
    click.echo("\nUsage in configuration file:")
    click.echo("\n  results_processing:")
    click.echo("    postprocessing:")
    click.echo("    - rosbags_tf_to_csv:")
    click.echo("        frames: [base_link, map]")
    click.echo("    - rosbags_bt_to_csv")
    click.echo("    - command:")
    click.echo("        script: ../../../tools/custom_script.sh")
    click.echo("        args: [--arg, value]")
    click.echo("\nCommands without parameters can be simple strings.")
    click.echo("Commands with parameters use plugin name as key with parameters as dict.")


@results.command(name='publish-commands')
def list_publication_plugins():
    """List all available publication plugins.

    Shows plugin names that can be used in the ``results_processing.publication`` section
    of the configuration file, along with their descriptions and parameters.
    """
    plugins = load_publication_plugins()

    if not plugins:
        click.echo("No publication plugins available.")
        click.echo("\nPublication plugins can be registered as plugins.")
        click.echo("See documentation for how to add custom publication plugins.")
        return

    click.echo("Available publication plugins:")
    click.echo("=" * 70)

    # Sort by plugin name for consistent output
    for plugin_name in sorted(plugins.keys()):
        click.echo(f"\n{plugin_name}")

        # Try to get the function's docstring
        try:
            func = plugins[plugin_name]
            if func.__doc__:
                # Clean up docstring and display first line
                doc_lines = [line.strip() for line in func.__doc__.strip().split('\n') if line.strip()]
                if doc_lines:
                    click.echo(f"  Description: {doc_lines[0]}")
        except Exception:
            pass

    click.echo("\n" + "=" * 70)
    click.echo("\nUsage in configuration file:")
    click.echo("\n  results_processing:")
    click.echo("    publication:")
    click.echo("    - zip:")
    click.echo("        destination: archives/")
    click.echo("        exclude_filter:")
    click.echo("        - '*.pyc'")
    click.echo("\nPlugins without parameters can be simple strings.")
    click.echo("Plugins with parameters use plugin name as key with parameters as dict.")
