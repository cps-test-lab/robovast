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
from pathlib import Path

import click
import yaml

from robovast.client.errors import handle_cli_exception
from robovast.common import fmt_size as _fmt_size
from robovast.common.execution import is_campaign_dir
from robovast.results_processing import run_postprocessing
from robovast.results_processing.merge_results import merge_results
from robovast.results_processing.metadata import generate_campaign_metadata
from robovast.results_processing.postprocessing import load_postprocessing_plugins
from robovast.results_processing.publication import load_publication_plugins, run_publication


@click.group()
def results():
    """Work on a results directory on THIS machine.

    Every verb here names a path and none of them needs a login: this is the local half
    of the tool, for someone holding a results tree -- publishing it, generating its
    metadata and provenance, merging campaigns. It ships with the full ``robovast``
    distribution, so a client-only install does not have this group at all, which is the
    honest signal that none of it is a service operation.

    What acts on a *campaign* is ``vast campaign`` -- including postprocessing, which
    lives there because the service owns the lane the runs executed on. This group used
    to carry a second, in-process ``postprocess`` beside it; it could only ever see a
    local campaign, and one postprocessing path is the point.
    """


@results.command(name='publish')
@click.option('--results-dir', '-r', required=True, type=click.Path(),
              help='Directory containing run results.')
@click.option('--force', '-f', is_flag=True,
              help='Overwrite existing output files without prompting.')
@click.option('--skip-postprocessing', is_flag=True,
              help='Skip postprocessing and only run publication plugins.')
@click.option('--skip-upload', is_flag=True,
              help='Only run packaging plugins (e.g. zip); skip upload plugins (e.g. zenodo).')
@click.option('--campaign', '-i', default=None, metavar='CAMPAIGN',
              help='Only publish a single campaign directory '
                   '(e.g. navigation-2026-03-20-153630). '
                   'Without this, all campaigns are published.')
@click.option('--allow-opaque', is_flag=True,
              help='Publish even when an input cannot be identified. The exemption is recorded '
                   'in the dataset, so it is visible to whoever reads it rather than untraceable.')
@click.option('--override', '-o', default=None, metavar='VAST_FILE',
              help='Override the .vast file read for publication metadata instead of the one '
                   'found in <campaign-name>-<timestamp>/_config/')
def publish_cmd(results_dir, force, skip_postprocessing, skip_upload, campaign, allow_opaque,
                override):
    """Publish run results using configured publication plugins.

    Executes postprocessing plugins (unless ``--skip-postprocessing`` is used)
    followed by publication plugins defined in the .vast file found in the
    most recent ``<campaign-name>-<timestamp>/_config/`` directory of the results directory.
    Publication plugins handle packaging and distribution of results.

    Use ``--override <file>`` to read metadata from a source .vast file instead
    of the campaign copy (e.g. after updating description or license).
    Use --force to overwrite existing output files without prompting.
    Use --skip-postprocessing to only run publication without postprocessing.
    Use --skip-upload to only run packaging plugins and skip upload plugins.
    Use --campaign / -i to restrict publication to a single campaign directory.

    """
    # `--override` is the one way to name a .vast here; there is no ambient project and
    # no second channel that could disagree with it.
    vast_file = override

    # Validate --campaign when provided
    if campaign is not None:
        campaign_path = Path(results_dir) / campaign
        if not campaign_path.is_dir():
            raise click.UsageError(
                f"Campaign directory not found: {campaign_path}"
            )
        if not is_campaign_dir(campaign):
            raise click.UsageError(
                f"'{campaign}' does not look like a campaign directory "
                "(expected pattern: <name>-YYYY-MM-DD-HHMMSS)."
            )

    click.echo("Starting publication...")
    click.echo(f"Results directory: {results_dir}")
    if campaign:
        click.echo(f"Campaign filter: {campaign}")
    if vast_file:
        click.echo(f"Using .vast file: {vast_file}")
    click.echo("-" * 60)

    # Run postprocessing first (unless skipped)
    if not skip_postprocessing:
        click.echo("Running postprocessing...")
        pp_success, pp_message = run_postprocessing(
            results_dir=results_dir,
            campaign=campaign,
            output_callback=click.echo,
            vast_file=vast_file,
        )
        click.echo()
        if not pp_success:
            click.echo("\n" + "=" * 60)
            click.echo(f"✗ Postprocessing failed: {pp_message}")
            click.echo("=" * 60)
            sys.exit(1)

    # Run publication
    success, message = run_publication(
        allow_opaque=allow_opaque,
        results_dir=results_dir,
        output_callback=click.echo,
        vast_file=vast_file,
        force=force,
        skip_upload=skip_upload,
        campaign=campaign,
    )

    click.echo("\n" + "=" * 60)
    if not success:
        click.echo(f"\u2717 {message}", err=True)
        sys.exit(1)
    click.echo(f"\u2713 {message}")


@results.command(name='backfill-provenance')
@click.argument('results_dir', type=click.Path(exists=True))
@click.option('--write', is_flag=True,
              help='Actually write. Without this, report what would change and touch nothing.')
@click.option('--force', is_flag=True,
              help='Re-derive a block an earlier run already wrote.')
def backfill_provenance_cmd(results_dir, write, force):
    """Derive what an old campaign's provenance can still be recovered from.

    Campaigns that ran before a field existed do not have it, and there is no way back to the
    moment it was knowable. Some of it is still derivable, though, and gets less so over time:
    a short revision resolves to a full one while the commit is reachable, and a tag resolves
    to a digest while the registry still serves it.

    Only ever ADDS, under a ``backfilled:`` block. A campaign's own record is evidence, so
    nothing it wrote is replaced -- otherwise a reader can no longer tell which values the
    campaign reported and which were inferred later. What cannot be derived is recorded as
    unknown with the reason, because "nobody could tell" and "nobody looked" are different
    answers.

    Dry by default: this runs over published data.
    """
    from robovast.common.backfill import (  # pylint: disable=import-outside-toplevel
        apply_backfill, plan_backfill)

    root = Path(results_dir)
    campaigns = sorted(d for d in root.iterdir() if d.is_dir() and is_campaign_dir(d.name))
    if not campaigns:
        click.echo(f"no campaign directories under {root}")
        return

    changed = skipped = 0
    for campaign in campaigns:
        plan = apply_backfill(campaign, force=force) if write else plan_backfill(campaign)
        if plan.get("unavailable"):
            click.echo(f"{campaign.name}: {click.style('skipped', fg='yellow')} "
                       f"-- {plan['unavailable']}")
            skipped += 1
            continue
        if plan["already_present"] and not force:
            click.echo(f"{campaign.name}: already backfilled")
            skipped += 1
            continue

        revision = plan["derived"]["robovast_revision"]
        images = plan["derived"]["images"]
        click.echo(f"{campaign.name}:")
        if revision.get("value"):
            click.echo(f"  revision  {revision['value']} ({revision['source']})")
        else:
            click.echo(f"  revision  {click.style('unknown', fg='yellow')} "
                       f"-- {revision['unknown']}")
        for role, entry in sorted((images.get("per_role") or {}).items()):
            if entry.get("value"):
                click.echo(f"  {role:<9} {entry['source']}")
            else:
                click.echo(f"  {role:<9} {click.style('unknown', fg='yellow')} "
                           f"-- {entry['unknown']}")
        changed += 1

    click.echo("")
    if write:
        click.echo(f"wrote {changed} campaign(s), skipped {skipped}")
    else:
        click.echo(f"{changed} campaign(s) would change, {skipped} skipped. "
                   f"Nothing written -- add --write.")


@results.command(name='merge-campaigns')
@click.argument('merged_campaign_dir', type=click.Path())
@click.option('--results-dir', '-r', default=None,
              help='Source directory containing run-\\* directories (uses project results directory if not specified)')
def merge_results_cmd(merged_campaign_dir, results_dir):
    """Merge campaign directories with identical configs into one merged_campaign_dir.

    Groups campaign-directory/config-directory by config_identifier from config.yaml.
    Run folders (0, 1, 2, ...) from all campaigns are renumbered and copied.
    Original campaign directories are not modified.

    """
    source_dir = results_dir

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


@results.command(name='generate-metadata')
@click.option('--results-dir', '-r', required=True, type=click.Path(),
              help='Directory containing run results.')
@click.option('--dot-pdf', is_flag=True, default=False,
              help='Also generate Graphviz DOT and PDF visualizations of the FAIR metadata graph.')
def generate_metadata_cmd(results_dir, dot_pdf):
    """Generate metadata.yaml and FAIR/PROV-O provenance metadata for all campaigns.

    First generates (or regenerates) ``metadata.yaml`` for each campaign via
    the standard metadata pipeline, then produces the compact JSON-LD
    provenance graph ``metadata.prov.json``.  Optionally also writes
    ``metadata.dot`` and renders ``metadata.pdf`` via Graphviz
    (requires ``dot`` on PATH).

    """
    results_path = Path(results_dir)
    if not results_path.is_dir():
        raise click.ClickException(f"Results directory does not exist: {results_dir}")

    campaign_dirs = sorted(
        d for d in results_path.iterdir()
        if d.is_dir() and is_campaign_dir(d.name)
    )
    if not campaign_dirs:
        raise click.ClickException(f"No campaign directories found in {results_dir}")

    click.echo("Generating metadata...")
    click.echo(f"Results directory: {results_dir}")
    if dot_pdf:
        click.echo("DOT/PDF visualization: enabled")
    click.echo("-" * 60)

    # Phase 1: generate metadata.yaml for all campaigns
    click.echo("Generating metadata.yaml...")
    try:
        meta_success, meta_msg = generate_campaign_metadata(
            str(results_dir),
            output_callback=lambda msg: click.echo(f"  {msg}"),
        )
        if not meta_success:
            raise click.ClickException(f"metadata.yaml generation failed: {meta_msg}")
        click.echo(f"  ✓ {meta_msg}")
    except click.ClickException:
        raise
    except Exception as e:  # pylint: disable=broad-except
        raise click.ClickException(f"metadata.yaml generation failed: {e}") from e

    click.echo("-" * 60)
    click.echo("Generating FAIR/PROV-O metadata (metadata.prov.json)...")

    errors = []
    for campaign_dir in campaign_dirs:
        metadata_yaml = campaign_dir / "metadata.yaml"
        if not metadata_yaml.is_file():
            click.echo(f"  Skipping {campaign_dir.name}: metadata.yaml not found")
            continue

        with open(metadata_yaml, "r", encoding="utf-8") as f:
            metadata = yaml.safe_load(f)

        click.echo(f"  Processing {campaign_dir.name}...")
        try:
            # Deferred: `rdflib`/`pyld` are the `fair` extra, and this module is a CLI
            # plugin loaded on every `vast` invocation -- importing them at module level
            # would make the whole `results` group vanish wherever they are not installed.
            from robovast.results_processing.fair_metadata import \
                generate_prov_metadata  # pylint: disable=import-outside-toplevel
            success, message = generate_prov_metadata(
                campaign_dir, metadata, generate_visualization=dot_pdf
            )
            if success:
                click.echo(f"  ✓ {message}")
            else:
                click.echo(f"  ✗ {message}", err=True)
                errors.append(campaign_dir.name)
        except Exception as e:  # pylint: disable=broad-except
            click.echo(f"  ✗ {campaign_dir.name}: {e}", err=True)
            errors.append(campaign_dir.name)

    click.echo("\n" + "=" * 60)
    if errors:
        click.echo(f"✗ Metadata generation failed for: {', '.join(errors)}", err=True)
        sys.exit(1)
    click.echo(f"✓ Metadata generated for {len(campaign_dirs)} campaign(s)")


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
