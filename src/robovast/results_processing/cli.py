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

import fnmatch
import os
import sys
import tarfile
import time
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv

from robovast.common import fmt_size as _fmt_size, make_download_progress_callback
from robovast.common.cli import get_project_config, handle_cli_exception
from robovast.common.cli.project_config import ProjectConfig
from robovast.common.execution import is_campaign_dir
from robovast.results_processing.merge_results import merge_results
from robovast.execution.cluster_execution.share_providers import \
    load_share_provider_plugins
from robovast.results_processing import run_postprocessing
from robovast.results_processing.fair_metadata import generate_prov_metadata
from robovast.results_processing.metadata import generate_campaign_metadata
from robovast.results_processing.postprocessing import \
    load_postprocessing_plugins
from robovast.results_processing.publication import (load_publication_plugins,
                                                     run_publication)


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
def postprocess_cmd(results_dir, force, override, debug, skip_rosout, skip_plugins, skip_db, skip_metadata):
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
                   'found in <campaign-name>-<timestamp>/_config/')
@click.option('--force', '-f', is_flag=True,
              help='Overwrite existing output files without prompting.')
@click.option('--skip-postprocessing', is_flag=True,
              help='Skip postprocessing and only run publication plugins.')
def publish_cmd(results_dir, override, force, skip_postprocessing):
    """Publish run results using configured publication plugins.

    Executes postprocessing plugins (unless ``--skip-postprocessing`` is used)
    followed by publication plugins defined in the .vast file found in the
    most recent ``<campaign-name>-<timestamp>/_config/`` directory of the results directory.
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


def _load_share_dotenv() -> None:
    """Load ``.env`` using the same search order as ``cluster upload-to-share``."""
    project_file = ProjectConfig.find_project_file()
    if project_file:
        project_dir = os.path.dirname(os.path.abspath(project_file))
        pc = ProjectConfig.load()
        if pc and pc.config_path:
            load_dotenv(os.path.join(os.path.dirname(pc.config_path), ".env"), override=False)
        load_dotenv(os.path.join(project_dir, ".env"), override=False)
    else:
        load_dotenv(override=False)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

@results.command(name='download')
@click.option('--output', '-o', default=None,
              help='Directory to extract results into (uses project results dir if not specified)')
@click.option('--campaign', '-i', 'campaigns', multiple=True,
              help='Only download this campaign (e.g. dynamic_obstacle-2025-02-27-123456 or campaign-2025-02-27-123456). '
                   'Can be specified multiple times. Without this, downloads all campaigns.')
@click.option('--force', '-f', is_flag=True,
              help='Re-download and re-extract even if the campaign directory already exists')
@click.option('--keep-archive', is_flag=True,
              help='Keep the downloaded .tar.gz file after extraction')
@click.option('--debug', is_flag=True,
              help='Print HTTP request/response details (URL, status, headers) for debugging')
def download_from_share_cmd(output, campaigns, force, keep_archive, debug):
    """Download campaign archives from the configured share service.

    Reads the same ``.env`` configuration as ``cluster upload-to-share``.
    For each ``<campaign-name>-<timestamp>.tar.gz`` found on the share the command:

    \b
    1. Checks whether the campaign directory already exists locally
       (skips the download if it does, unless ``--force`` is given).
    2. Streams the archive to a temporary file with a live progress bar.
    3. Extracts the archive into the output directory.
    4. Removes the temporary archive (unless ``--keep-archive``).

    Required ``.env`` variables:

    \b
    ROBOVAST_SHARE_TYPE      — share provider (e.g. ``gcs``, ``webdav``, ``sftp``)
    ROBOVAST_GCS_BUCKET      — GCS bucket name         (when ROBOVAST_SHARE_TYPE=gcs)
    ROBOVAST_WEBDAV_URL      — WebDAV collection URL   (when ROBOVAST_SHARE_TYPE=webdav)
    ROBOVAST_WEBDAV_USER     — WebDAV username          (when ROBOVAST_SHARE_TYPE=webdav)
    ROBOVAST_WEBDAV_PASSWORD — WebDAV password          (when ROBOVAST_SHARE_TYPE=webdav)
    """
    _load_share_dotenv()

    if debug:
        import logging  # pylint: disable=import-outside-toplevel
        import http.client as http_client  # pylint: disable=import-outside-toplevel
        http_client.HTTPConnection.debuglevel = 1
        logging.basicConfig()
        logging.getLogger().setLevel(logging.DEBUG)
        requests_log = logging.getLogger("urllib3")
        requests_log.setLevel(logging.DEBUG)
        requests_log.propagate = True
        click.echo("[debug] HTTP debug logging enabled", err=True)

    share_type = os.environ.get("ROBOVAST_SHARE_TYPE", "").strip()
    if not share_type:
        raise click.UsageError(
            "ROBOVAST_SHARE_TYPE is not set.\n"
            "Add it to a .env file in your project directory.\n"
            "Example:\n"
            "  ROBOVAST_SHARE_TYPE=gcs\n"
            "  ROBOVAST_GCS_BUCKET=my-robovast-results"
        )

    providers = load_share_provider_plugins()
    if share_type not in providers:
        available = ", ".join(sorted(providers)) or "(none installed)"
        raise click.UsageError(
            f"Unknown share type '{share_type}'.\n"
            f"Available providers: {available}"
        )

    try:
        provider = providers[share_type]()
    except click.UsageError:
        raise
    except Exception as exc:
        handle_cli_exception(exc)
        return

    # Resolve output directory
    if output is None:
        raw_config = ProjectConfig.load()
        if not raw_config or not raw_config.results_dir:
            raise click.ClickException(
                "Project not initialized. Run 'vast init <config-file>' first, "
                "or pass --output explicitly."
            )
        output = raw_config.results_dir

    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)

    # List available archives
    click.echo(f"Listing campaigns on {share_type}...")
    try:
        archives = provider.list_campaign_archives()
    except NotImplementedError as exc:
        raise click.UsageError(str(exc)) from exc
    except click.UsageError:
        raise
    except Exception as exc:
        handle_cli_exception(exc)
        return

    if not archives:
        click.echo("No campaign archives found on the share.")
        return

    # Filter by requested campaign IDs
    requested = set(campaigns)
    if requested:
        def _archive_id(name: str) -> str:
            # Strip leading prefix (e.g. "results/") and trailing ".tar.gz"
            base = os.path.basename(name)
            return base[: -len(".tar.gz")] if base.endswith(".tar.gz") else base

        archives = [a for a in archives if _archive_id(a) in requested]
        if not archives:
            raise click.UsageError(
                f"None of the requested campaigns were found on the share.\n"
                f"Requested: {', '.join(sorted(requested))}"
            )

    downloaded = 0
    skipped = 0

    for object_name in archives:
        base = os.path.basename(object_name)
        campaign_id = base[: -len(".tar.gz")] if base.endswith(".tar.gz") else base
        campaign_dir = output_path / campaign_id

        if campaign_dir.exists() and not force:
            click.echo(f"  {campaign_id}  already exists, skipping (use --force to re-download)")
            skipped += 1
            continue

        # Use a deterministic partial-download path so we can resume
        tmp_path = str(output_path / f".{base}.part")
        resume_offset = 0
        if os.path.exists(tmp_path):
            resume_offset = os.path.getsize(tmp_path)
            click.echo(f"  {campaign_id}  resuming from {resume_offset / 1024 / 1024:.1f} MiB...")
        else:
            click.echo(f"  {campaign_id}  downloading...")

        start = time.monotonic()
        progress_cb = make_download_progress_callback(campaign_id, start)

        # Keep the .part file when the download fails mid-transfer so the
        # next invocation can resume from where it left off.
        download_complete = False
        try:
            try:
                provider.download_archive(
                    object_name, tmp_path, progress_cb,
                    resume_offset=resume_offset,
                )
            except Exception as exc:
                if isinstance(exc, (click.UsageError, click.ClickException)):
                    raise
                handle_cli_exception(exc)
                continue
            finally:
                sys.stdout.write("\n")
                sys.stdout.flush()

            # Extract
            click.echo(f"  {campaign_id}  extracting...")
            try:
                with tarfile.open(tmp_path, "r:gz") as tf:
                    tf.extractall(output_path)
            except tarfile.TarError as exc:
                raise click.ClickException(
                    f"Failed to extract '{base}': {exc}"
                ) from exc

            elapsed = time.monotonic() - start
            size_mib = os.path.getsize(tmp_path) / 1024 / 1024
            click.echo(
                f"  {campaign_id}  ✓  {size_mib:.1f} MiB in {elapsed:.0f}s"
            )
            downloaded += 1

            if keep_archive:
                dest_archive = output_path / base
                os.replace(tmp_path, dest_archive)
                tmp_path = ""  # don't delete below
            download_complete = True
        finally:
            # Only remove the .part file after a fully successful
            # download+extraction.  Any other exit path — network error,
            # click exception, or Ctrl+C (KeyboardInterrupt) — leaves the
            # partial file in place so the next run can resume.
            if tmp_path and os.path.exists(tmp_path) and download_complete:
                os.unlink(tmp_path)

    click.echo()
    parts = [f"✓ Downloaded {downloaded} campaign(s)"]
    if skipped:
        parts.append(f"{skipped} skipped")
    click.echo("  ".join(parts))


@results.command(name='list-share')
@click.option('--campaign', '-i', 'campaigns', multiple=True,
              help='Only show specific campaigns (e.g. campaign-2025-02-27-123456). '
                   'Can be specified multiple times. Without this, shows all campaigns.')
def list_share_cmd(campaigns):
    """List campaign archives on the configured share service with sizes.

    Reads the same ``.env`` configuration as ``cluster upload-to-share``.
    Prints one line per archive with its size on the share.

    Required ``.env`` variables:

    \b
    ROBOVAST_SHARE_TYPE  — share provider (e.g. ``gcs``)
    ROBOVAST_GCS_BUCKET  — GCS bucket name  (when ROBOVAST_SHARE_TYPE=gcs)
    """
    _load_share_dotenv()

    share_type = os.environ.get("ROBOVAST_SHARE_TYPE", "").strip()
    if not share_type:
        raise click.UsageError(
            "ROBOVAST_SHARE_TYPE is not set.\n"
            "Add it to a .env file in your project directory.\n"
            "Example:\n"
            "  ROBOVAST_SHARE_TYPE=gcs\n"
            "  ROBOVAST_GCS_BUCKET=my-robovast-results"
        )

    providers = load_share_provider_plugins()
    if share_type not in providers:
        available = ", ".join(sorted(providers)) or "(none installed)"
        raise click.UsageError(
            f"Unknown share type '{share_type}'.\n"
            f"Available providers: {available}"
        )

    try:
        provider = providers[share_type]()
    except click.UsageError:
        raise
    except Exception as exc:
        handle_cli_exception(exc)
        return

    click.echo(f"Listing campaigns on {share_type}...")
    try:
        archives = provider.list_campaign_archives_with_size()
    except NotImplementedError as exc:
        raise click.UsageError(str(exc)) from exc
    except click.UsageError:
        raise
    except Exception as exc:
        handle_cli_exception(exc)
        return

    if not archives:
        click.echo("No campaign archives found on the share.")
        return

    # Filter by requested campaign IDs
    if campaigns:
        requested = set(campaigns)

        def _archive_id(name: str) -> str:
            base = os.path.basename(name)
            return base[: -len(".tar.gz")] if base.endswith(".tar.gz") else base

        archives = [(n, s) for n, s in archives if _archive_id(n) in requested]
        if not archives:
            raise click.UsageError(
                f"None of the requested campaigns were found on the share.\n"
                f"Requested: {', '.join(sorted(campaigns))}"
            )

    total_size = 0
    for object_name, size in sorted(archives):
        base = os.path.basename(object_name)
        campaign_id = base[: -len(".tar.gz")] if base.endswith(".tar.gz") else base
        size_str = _fmt_size(size) if size >= 0 else "unknown size"
        click.echo(f"  {campaign_id}  {size_str}")
        if size >= 0:
            total_size += size

    click.echo()
    known_sizes = [(n, s) for n, s in archives if s >= 0]
    if known_sizes:
        click.echo(f"  {len(archives)} campaign(s)  total {_fmt_size(total_size)}")
    else:
        click.echo(f"  {len(archives)} campaign(s)")


@results.command(name='remove-from-share')
@click.option('--campaign', '-i', 'campaigns', multiple=True, required=True,
              help='Campaign to remove (e.g. campaign-2025-02-27-123456 or '
                   'campaign-2026-03-09-*). Can be specified multiple times. '
                   'Wildcards (* ? [...]) are supported.')
@click.option('--yes', '-y', is_flag=True,
              help='Skip confirmation prompt')
def remove_from_share_cmd(campaigns, yes):
    """Remove campaign archives from the configured share service.

    Reads the same ``.env`` configuration as ``cluster upload-to-share``.
    Each named campaign archive is permanently deleted from the share.
    Wildcards (``*``, ``?``, ``[…]``) are supported in campaign names;
    e.g. ``campaign-2026-03-09-*`` removes all campaigns from that day.

    Required ``.env`` variables:

    \b
    ROBOVAST_SHARE_TYPE  — share provider (e.g. ``gcs``)
    ROBOVAST_GCS_BUCKET  — GCS bucket name  (when ROBOVAST_SHARE_TYPE=gcs)
    ROBOVAST_GCS_KEY_FILE — service-account key file with delete permission
    """
    _load_share_dotenv()

    share_type = os.environ.get("ROBOVAST_SHARE_TYPE", "").strip()
    if not share_type:
        raise click.UsageError(
            "ROBOVAST_SHARE_TYPE is not set.\n"
            "Add it to a .env file in your project directory.\n"
            "Example:\n"
            "  ROBOVAST_SHARE_TYPE=gcs\n"
            "  ROBOVAST_GCS_BUCKET=my-robovast-results"
        )

    providers = load_share_provider_plugins()
    if share_type not in providers:
        available = ", ".join(sorted(providers)) or "(none installed)"
        raise click.UsageError(
            f"Unknown share type '{share_type}'.\n"
            f"Available providers: {available}"
        )

    try:
        provider = providers[share_type]()
    except click.UsageError:
        raise
    except Exception as exc:
        handle_cli_exception(exc)
        return

    # List archives to resolve object names for the requested campaign IDs
    click.echo(f"Listing campaigns on {share_type}...")
    try:
        all_archives = provider.list_campaign_archives_with_size()
    except NotImplementedError as exc:
        raise click.UsageError(str(exc)) from exc
    except click.UsageError:
        raise
    except Exception as exc:
        handle_cli_exception(exc)
        return

    def _archive_id(name: str) -> str:
        base = os.path.basename(name)
        return base[: -len(".tar.gz")] if base.endswith(".tar.gz") else base

    def _is_glob(pattern: str) -> bool:
        return any(c in pattern for c in ("*", "?", "["))

    # Match each pattern (exact or glob) against all archive IDs
    matched: dict[str, tuple[str, int]] = {}  # archive_id -> (object_name, size)
    for archive_name, size in all_archives:
        aid = _archive_id(archive_name)
        for pattern in campaigns:
            if fnmatch.fnmatch(aid, pattern):
                matched[aid] = (archive_name, size)
                break

    to_remove = list(matched.values())

    # Report patterns that matched nothing
    unmatched_exact = [p for p in campaigns if not _is_glob(p) and not any(
        fnmatch.fnmatch(_archive_id(n), p) for n, _ in all_archives
    )]
    unmatched_glob = [p for p in campaigns if _is_glob(p) and not any(
        fnmatch.fnmatch(_archive_id(n), p) for n, _ in all_archives
    )]
    if unmatched_exact:
        raise click.UsageError(
            f"Campaign(s) not found on the share: {', '.join(sorted(unmatched_exact))}\n"
            "Use 'vast results list-share' to see available campaigns."
        )
    for pattern in unmatched_glob:
        click.echo(f"  Warning: no campaigns matched pattern '{pattern}'")

    if not to_remove:
        click.echo("No campaigns to remove.")
        return

    if not yes:
        click.echo()
        for object_name, size in sorted(to_remove):
            size_str = f"  ({_fmt_size(size)})" if size >= 0 else ""
            click.echo(f"  {_archive_id(object_name)}{size_str}")
        click.echo()
        click.confirm(
            f"Remove {len(to_remove)} campaign archive(s) from {share_type}?",
            abort=True,
        )

    removed = 0
    for object_name, _size in sorted(to_remove):
        campaign_id = _archive_id(object_name)
        click.echo(f"  {campaign_id}  removing...")
        try:
            provider.remove_archive(object_name)
        except NotImplementedError as exc:
            raise click.UsageError(str(exc)) from exc
        except click.UsageError:
            raise
        except Exception as exc:
            handle_cli_exception(exc)
            continue
        click.echo(f"  {campaign_id}  ✓ removed")
        removed += 1

    click.echo()
    click.echo(f"✓ Removed {removed} campaign archive(s) from {share_type}.")
