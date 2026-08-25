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
import time
from pathlib import Path

import click
import yaml

from robovast.client.errors import handle_cli_exception
from robovast.client.project_config import ProjectConfig, get_project_config
from robovast.common import fmt_size as _fmt_size
from robovast.common import make_transfer_progress_callback
from robovast.common.execution import is_campaign_dir
from robovast.results_processing import run_postprocessing
from robovast.results_processing.merge_results import merge_results
from robovast.results_processing.metadata import generate_campaign_metadata
from robovast.results_processing.postprocessing import load_postprocessing_plugins
from robovast.results_processing.publication import load_publication_plugins, run_publication


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
                   'found in <campaign-name>-<timestamp>/_config/')
@click.option('--debug', is_flag=True,
              help='Show full plugin output (stdout) for each postprocessing step.')
@click.option('--skip-rosout', is_flag=True,
              help='Skip rosout bag processing.')
@click.option('--skip', 'skip_plugins', multiple=True, metavar='PLUGIN',
              help='Skip a postprocessing plugin defined in the .vast file '
                   '(e.g. --skip rosbags_to_webm). Can be specified multiple times.')
@click.option('--skip-db', is_flag=True,
              help='Skip data.db creation.')
@click.option('--skip-metadata', is_flag=True,
              help='Skip metadata.yaml generation.')
@click.option('--campaign', '-i', default=None, metavar='CAMPAIGN',
              help='Only (re)process a single campaign directory '
                   '(e.g. navigation-2026-03-20-153630) instead of the most recent.')
def postprocess_cmd(results_dir, force, override, debug, skip_rosout, skip_plugins,
                    skip_db, skip_metadata, campaign):
    """Run postprocessing commands on run results.

    Executes postprocessing commands defined in the .vast file found in the
    most recent ``<campaign-name>-<timestamp>/_config/`` directory of the results directory.
    Postprocessing is skipped if the result-directory is unchanged,
    unless --force is specified.

    Use --override to supply a .vast file explicitly instead of the campaign copy.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    # Resolve results_dir from project config when not explicitly provided.
    # postprocess never uses config_path from the project file (it always reads
    # the .vast from <campaign-name>-<timestamp>/_config/ or --override), so only results_dir
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
        debug=debug,
        skip_rosout=skip_rosout,
        skip=list(skip_plugins),
        skip_db=skip_db,
        skip_metadata=skip_metadata,
        campaign=campaign,
    )

    click.echo("\n" + "=" * 60)
    if not success:
        click.echo(f"\u2717 {message}", err=True)
        sys.exit(1)


@results.command(name='publish')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing run results (uses project results dir if not specified)')
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
def publish_cmd(results_dir, force, skip_postprocessing, skip_upload, campaign, allow_opaque):
    """Publish run results using configured publication plugins.

    Executes postprocessing plugins (unless ``--skip-postprocessing`` is used)
    followed by publication plugins defined in the .vast file found in the
    most recent ``<campaign-name>-<timestamp>/_config/`` directory of the results directory.
    Publication plugins handle packaging and distribution of results.

    Use ``vast -V <file> results publish`` to read metadata from the source
    .vast file instead of the campaign copy (e.g. after updating description
    or license).
    Use --force to overwrite existing output files without prompting.
    Use --skip-postprocessing to only run publication without postprocessing.
    Use --skip-upload to only run packaging plugins and skip upload plugins.
    Use --campaign / -i to restrict publication to a single campaign directory.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    # Pick up the .vast file from the global -V / --vast-file option if given
    vast_file = None
    _ctx = click.get_current_context(silent=True)
    if _ctx and _ctx.obj:
        vast_file = _ctx.obj.get('vast_file')

    # Resolve results_dir from project config when not explicitly provided.
    if results_dir is None:
        raw_config = ProjectConfig.load()
        if not raw_config or not raw_config.results_dir:
            raise click.ClickException(
                "Project not initialized. Run 'vast init <config-file>' first."
            )
        results_dir = raw_config.results_dir

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


@results.command(name='import')
@click.argument('archive', type=click.Path(exists=True, dir_okay=False))
@click.option('--force', is_flag=True, help='Replace a campaign of the same id already there.')
@click.option('--rebuild-store', is_flag=True,
              help='Reconstruct campaign.db from the results tree (the recovery for a corrupt one).')
def import_cmd(archive, force, rebuild_store):
    """Take a campaign archive into the service, and postprocess it if it needs it.

    ARCHIVE is a ``.tar.gz`` on *this* machine -- one ``vast results download`` or ``vast
    share download`` produced, or a colleague sent. It is uploaded to the service and
    imported there, so the campaign lands where the web UI and every other client can see
    it; the file itself is never deleted, it is yours.

    Importing is more than extracting: listings and the web UI answer from ``campaign.db``,
    not from the results tree, so a campaign that is only unpacked is invisible. And when
    the archive is a **raw** one -- no ``_execution/data.db``, which is what the share holds
    -- postprocessing is chained automatically, because a campaign without its metric tables
    is not one you can ask anything.

    Long-running, so it returns once the import is under way: the campaign appears
    immediately at phase ``importing``. Watch it with ``vast wait <campaign-id>``, or in the
    campaign view.

    There is no local-only mode. Import means "into a service" -- that is where the tracked
    phase, the log and the chained postprocessing are. A results directory with no service
    is postprocessed in place with ``vast results postprocess -r <dir>``.
    """
    from robovast.client.service_target import (  # pylint: disable=import-outside-toplevel
        echo_target, service_client)
    from robovast.service.interface import \
        ImportCampaignRequest  # pylint: disable=import-outside-toplevel
    from robovast.service.project_push import \
        push_campaign_archive  # pylint: disable=import-outside-toplevel

    path = Path(archive)
    with service_client(require_service=True) as (client, label):
        echo_target(label)
        click.echo(f"uploading {path.name} ({_fmt_size(path.stat().st_size)}) ...")
        staged = push_campaign_archive(client, path)
        ref = client.import_campaign(ImportCampaignRequest(
            archive_path=staged, force=force, rebuild_store=rebuild_store))

    click.echo(f"\u2713 importing {ref.campaign_id}")
    if ref.note:
        click.echo(f"  {ref.note}")
    click.echo(f"  watch it with: vast wait {ref.campaign_id}")


@results.command(name='backfill-provenance')
@click.argument('results_dir', required=False, type=click.Path(exists=True))
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

    if results_dir is None:
        try:
            results_dir = get_project_config().results_dir
        except Exception as e:  # noqa: BLE001
            handle_cli_exception(e)
            return

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


@results.command(name='generate-metadata')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing run results (uses project results dir if not specified)')
@click.option('--dot-pdf', is_flag=True, default=False,
              help='Also generate Graphviz DOT and PDF visualizations of the FAIR metadata graph.')
def generate_metadata_cmd(results_dir, dot_pdf):
    """Generate metadata.yaml and FAIR/PROV-O provenance metadata for all campaigns.

    First generates (or regenerates) ``metadata.yaml`` for each campaign via
    the standard metadata pipeline, then produces the compact JSON-LD
    provenance graph ``metadata.prov.json``.  Optionally also writes
    ``metadata.dot`` and renders ``metadata.pdf`` via Graphviz
    (requires ``dot`` on PATH).

    Requires project initialization with ``vast init`` first (unless
    ``--results-dir`` is specified).
    """
    if results_dir is None:
        raw_config = ProjectConfig.load()
        if not raw_config or not raw_config.results_dir:
            raise click.ClickException(
                "Project not initialized. Run 'vast init <config-file>' first."
            )
        results_dir = raw_config.results_dir

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


@results.command(name='download')
@click.argument('campaigns', nargs=-1, required=True)
@click.option('--output', '-o', 'output', default=None, type=click.Path(file_okay=False),
              help='Directory to write the archives into [default: the current directory]')
@click.option('--force', '-f', is_flag=True,
              help='Overwrite an archive of the same name that is already here')
def download_cmd(campaigns, output, force):
    """Download campaign archives from the service, one ``.tar.gz`` each.

    That is the whole command: it fetches ``<campaign-id>.tar.gz`` and stops. Nothing
    is extracted, no results directory is written into, and no state is kept about what
    you already have -- the archive is yours, to keep, copy, unpack, or hand back with
    ``vast results import``.

    The archive is the campaign as the service holds it, postprocessing and all. The
    share's raw, pre-postprocess snapshot is a different system with different
    credentials: ``vast share download``.

    Writes into the current directory unless ``-o`` says otherwise -- an archive is a
    file, not a results tree, so the project's results directory is the wrong home
    for it.
    """
    from robovast.client.service_target import \
        service_client  # pylint: disable=import-outside-toplevel
    from robovast.service.project_push import \
        download_campaign_archive  # pylint: disable=import-outside-toplevel

    out_dir = Path(output) if output else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    with service_client(require_service=True) as (client, label):
        click.echo(f"Downloading {len(campaigns)} campaign archive(s) from {label} ...")
        written = skipped = 0
        for campaign_id in campaigns:
            dest = out_dir / f"{campaign_id}.tar.gz"
            if dest.exists() and not force:
                click.echo(f"  {dest.name}  already here, skipping "
                           "(use --force to re-download)")
                skipped += 1
                continue
            start = time.monotonic()
            try:
                download_campaign_archive(
                    client, campaign_id, str(dest),
                    progress_callback=make_transfer_progress_callback(campaign_id, start))
            # Ahead of the broad handler below, which would otherwise swallow click's own
            # control flow and report a usage error as an unexpected failure.
            except (click.UsageError, click.ClickException):  # pylint: disable=try-except-raise
                raise
            except Exception as exc:  # noqa: BLE001
                sys.stdout.write("\n")
                handle_cli_exception(exc)
                continue
            finally:
                sys.stdout.write("\n")
                sys.stdout.flush()
            click.echo(f"  {campaign_id}  \u2713  {_fmt_size(dest.stat().st_size)} "
                       f"in {time.monotonic() - start:.0f}s  ->  {dest}")
            written += 1

    click.echo()
    parts = [f"\u2713 Downloaded {written} archive(s)"]
    if skipped:
        parts.append(f"{skipped} skipped")
    click.echo("  ".join(parts))


def _require_service_client():
    """Resolve the reachable robovast-service client, or raise a clean UsageError.

    The service-routed campaign operations (reprocess, delete) all go through it —
    it owns the backend (local Docker / cluster + object store), so the CLI needs
    no kubeconfig or object-store credentials of its own.
    """
    from robovast.client.service_target import \
        detected_service_url  # pylint: disable=import-outside-toplevel
    url = detected_service_url()
    if not url:
        raise click.UsageError(
            "No robovast-service is reachable. Start one with 'vast serve' (local) "
            "or tunnel to a cluster service first.")
    from robovast.service.client import RobovastClient  # pylint: disable=import-outside-toplevel
    return RobovastClient(url)


@results.command(name='reprocess')
@click.argument('campaign', metavar='CAMPAIGN')
@click.option('--force', '-f', is_flag=True,
              help='Bypass per-rosbag caches and reprocess all bags.')
@click.option('--skip', 'skip_plugins', multiple=True, metavar='PLUGIN',
              help='Skip a postprocessing plugin (repeatable), e.g. --skip rosbags_to_webm.')
def reprocess_cmd(campaign, force, skip_plugins):
    """(Re)run analysis postprocessing for one CAMPAIGN via the robovast-service.

    The backend-neutral counterpart of ``vast results postprocess`` (which runs
    in-process against a local results dir): this is campaign-scoped and routes
    through the service, so it also drives a **cluster** campaign — the rosbag→CSV
    step runs in-cluster and ``data.db`` is rebuilt. Mirrors the web "Retrigger
    postprocessing" action and the MCP ``run_postprocessing`` tool.
    """
    from robovast.service.interface import \
        RunPostprocessingRequest  # pylint: disable=import-outside-toplevel
    client = _require_service_client()
    try:
        res = client.run_postprocessing(RunPostprocessingRequest(
            campaign_id=campaign, force=force, skip=list(skip_plugins)))
    except Exception as exc:
        handle_cli_exception(exc)
        return
    if not res.ok:
        raise click.ClickException(res.message or "postprocessing failed")
    click.echo(f"✓ {res.message or 'postprocessing complete'}")


@results.command(name='delete')
@click.argument('campaign', metavar='CAMPAIGN')
@click.option('--yes', '-y', is_flag=True, help='Skip the confirmation prompt.')
def delete_campaign_cmd(campaign, yes):
    """Permanently delete one CAMPAIGN wholesale via the robovast-service.

    Removes the campaign's durable home — its directory under the results root on a
    local service, or its object-store data (plus any leftover Kubernetes Jobs and
    the service's cache) on a cluster service. This is the full "forget this
    campaign" action; ``vast execution cluster download-cleanup`` only frees
    object-store buckets, and ``vast share remove`` only touches the
    external share (which this command leaves untouched).

    The service refuses a campaign that is still running — stop it first. This is
    irreversible.
    """
    if not yes and not click.confirm(
            f"Permanently delete campaign '{campaign}'? This cannot be undone."):
        click.echo("Aborted.")
        return
    client = _require_service_client()
    try:
        res = client.delete_campaign(campaign)
    except Exception as exc:
        handle_cli_exception(exc)
        return
    if not res.ok:
        raise click.ClickException(res.message or "delete failed")
    click.echo(f"✓ {res.message or f'Deleted {campaign}'}")
