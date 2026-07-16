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

"""Main CLI entry point for RoboVAST."""

import logging
import os
import shutil
import sys
import tarfile
from importlib.metadata import entry_points

import click

from ..common import load_config
from ..execution import is_campaign_dir
from ..logging_config import (get_logger, setup_logging,
                              setup_logging_from_project_config)
from .checks import check_docker_access
from .project_config import ProjectConfig, get_project_config

logger = get_logger(__name__)


def configure_logging(ctx, param, value):
    """Callback to configure logging based on --log-level option."""
    if value is not None:
        # User explicitly specified log level, use it
        setup_logging(value)
        # Store in context for later use
        ctx.ensure_object(dict)
        ctx.obj['log_level'] = value
    else:
        # No log level specified, use project config
        setup_logging_from_project_config()
    return value


@click.group()
@click.option('--log-level', '-l',
              type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], case_sensitive=False),
              help='Set logging level (overrides project configuration)',
              callback=configure_logging,
              is_eager=True,
              expose_value=False)
@click.option('--vast-file', '-V', type=click.Path(exists=True), default=None,
              help='Override the .vast configuration file (instead of project default)')
@click.version_option(package_name="robovast", prog_name="RoboVAST")
@click.pass_context
def cli(ctx, vast_file):
    """VAST - RoboVAST Command-Line Interface.

    Main command for managing variations, executing scenarios,
    and analyzing results in the RoboVAST framework.

    The global ``--log-level`` option can be used to control logging verbosity
    for any command, overriding the project configuration.

    Use ``--vast-file`` / ``-V`` to temporarily use a different ``.vast``
    configuration file instead of the one stored in the project.

    Examples:
      vast --log-level DEBUG execution cluster cleanup
      vast --log-level INFO init config.yaml
      vast -V other.vast config list
      vast -V other.vast exec cluster run

    See ``vast --help`` for a list of available commands.
    """
    # Ensure context object exists
    ctx.ensure_object(dict)
    if vast_file:
        ctx.obj['vast_file'] = os.path.abspath(vast_file)


@cli.command()
@click.argument('config', type=click.Path(exists=True))
@click.option('--results-dir', '-r', default="results", type=click.Path(),
              help='Directory for storing results')
@click.option('--project-log-level', default="INFO",
              type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], case_sensitive=False),
              help='Default logging level for the project (saved to project config)')
@click.option('--force', '-f', is_flag=True,
              help='Skip Docker and Kubernetes accessibility checks')
def init(config, results_dir, project_log_level, force):
    """Initialize a VAST project.

    Creates a `.vast_project` file in the current directory that stores
    the configuration file path, results directory, and default logging level.
    These settings will be used by other VAST commands automatically.

    The default log level can be overridden for any command using the global
    ``--log-level`` flag (e.g., ``vast --log-level DEBUG <command>``).

    By default, performs the following checks before initialization:

    * Docker daemon accessibility and version
    * Kubernetes cluster connectivity and version
    * robovast pod is running in the default namespace

    Use the ``--force`` flag to skip all these checks if needed.
    """
    # Check Docker and Kubernetes access unless --force is used
    # Check Docker access
    if force:
        click.echo("⚠ Warning: Skipping checks (--force enabled)")

    # check integrity of config file
    try:
        load_config(config)
    except Exception as e:
        click.echo(f"✗ Error: Failed to load configuration file: {e}", err=True)
        if not force:
            sys.exit(1)

    logger.debug("Checking Docker daemon access...")
    docker_ok, docker_msg = check_docker_access()
    if not docker_ok and not force:
        click.echo(f"✗ Error: {docker_msg}", err=True)
        click.echo("  Docker is required for RoboVAST execution.", err=True)
        sys.exit(1)

    # Convert to absolute paths
    project_file_dir = os.path.abspath(os.getcwd())
    if not os.path.isabs(config):
        config_path = os.path.abspath(os.path.join(project_file_dir, config))
    else:
        config_path = config
    if not os.path.isabs(results_dir):
        results_path = os.path.abspath(os.path.join(project_file_dir, results_dir))
    else:
        results_path = results_dir

    # Validate config file exists
    if not os.path.isfile(config_path):
        click.echo(f"✗ Error: Configuration file not found: {config_path}", err=True)
        sys.exit(1)

    # Create ProjectConfig and save it
    project_config = ProjectConfig(config_path=config_path, results_dir=results_path, log_level=project_log_level)

    # Validate the configuration
    is_valid, error = project_config.validate()
    if not is_valid:
        click.echo(f"✗ Error: {error}", err=True)
        sys.exit(1)

    # Check if .vast_project already exists
    existing_file = ProjectConfig.find_project_file()
    if existing_file:
        click.echo(f"⚠ Warning: Overwriting existing project file: {existing_file}")

    # Save the project file
    project_file = project_config.save()

    click.echo(f"✓ Project initialized successfully!")
    logging.debug(f"Configuration: {config_path}")
    logging.debug(f"Results directory: {results_path}")
    logging.debug(f"Log level: {project_log_level}")
    logging.debug(f"Project file: {project_file}")


def _ensure_ui_built(rebuild: bool = False) -> None:
    """(Re)build the web UI's ``ui/dist`` for a source checkout, when needed.

    No-op unless run from a source tree: a packaged install / in-cluster pod has
    no ``ui/`` sources (its dist is baked in and pointed at by ``ROBOVAST_UI_DIST``),
    so there is nothing — and no ``npm`` — to build. In a checkout, build only when
    ``ui/dist`` is missing or older than the UI sources (or when *rebuild*), so a
    normal ``vast serve`` costs just an mtime scan.
    """
    import subprocess
    from pathlib import Path
    if os.environ.get('ROBOVAST_UI_DIST'):
        return  # packaged/baked dist — nothing to build here
    ui_dir = Path(__file__).resolve().parents[4] / 'ui'
    if not (ui_dir / 'package.json').is_file():
        return  # not a source checkout
    dist_index = ui_dir / 'dist' / 'index.html'
    src_dir = ui_dir / 'src'
    fresh = dist_index.is_file() and src_dir.is_dir() and not any(
        p.stat().st_mtime > dist_index.stat().st_mtime
        for p in src_dir.rglob('*') if p.is_file())
    if fresh and not rebuild:
        return
    npm = shutil.which('npm')
    if npm is None:
        raise click.ClickException(
            "the web UI needs building but 'npm' was not found; install Node.js "
            "(or prebuild once with 'cd ui && npm run build')")
    if not (ui_dir / 'node_modules').is_dir():
        click.echo('Installing web UI dependencies (npm install)…')
        subprocess.run([npm, 'install'], cwd=str(ui_dir), check=True)  # noqa: S603
    click.echo('Building web UI (npm run build)…')
    subprocess.run([npm, 'run', 'build'], cwd=str(ui_dir), check=True)  # noqa: S603


@cli.command()
@click.option('--host', default='127.0.0.1', show_default=True,
              help='Interface to bind. Keep 127.0.0.1 unless behind a tunnel/proxy: '
                   'the service is unauthenticated in v1.')
@click.option('--port', default=8800, show_default=True, type=int,
              help='Port to listen on.')
@click.option('--backend', type=click.Choice(['auto', 'local', 'cluster']),
              default='auto', show_default=True,
              help="Execution backend. 'auto' picks 'cluster' when running inside "
                   "a Kubernetes pod, else 'local' Docker.")
@click.option('--rebuild-ui', is_flag=True,
              help='Force a web UI rebuild even if ui/dist looks up to date '
                   '(source checkout only).')
def serve(host, port, backend, rebuild_ui):
    """Run a persistent robovast-service (and its web UI).

    Starts the FastAPI service that the ``vast`` CLI, the MCP server, and the web
    UI drive campaigns through over HTTP — and campaigns survive client exit
    (unlike ``vast exec local run``). The service also serves the web UI at the
    same URL, so this is the one command to run it: from a source checkout it
    (re)builds ``ui/dist`` first when it is missing or stale (needs ``npm``;
    ``--rebuild-ui`` forces it). One binary, two backends:

    * **local** (default off-cluster) — local Docker + local filesystem (mode 2);
      run it on your machine or a remote VM reached over an SSH tunnel.
    * **cluster** (default in-pod) — launches a controller pod per campaign
      (mode 3); this is what the in-cluster ``robovast-service`` Deployment runs.

    Security: unauthenticated in v1, so it binds ``127.0.0.1`` by default and
    must stay behind localhost / SSH tunnel / port-forward (see
    ``docs/deployment.rst``). Web UI + OpenAPI docs at ``/`` and ``/docs``.
    """
    from robovast.service.app import serve as _serve

    # Build the SPA the service serves, so a source checkout needs one command
    # (no-op for a packaged/in-cluster install — see _ensure_ui_built).
    _ensure_ui_built(rebuild=rebuild_ui)

    if backend == 'auto':
        backend = 'cluster' if os.environ.get('KUBERNETES_SERVICE_HOST') else 'local'

    if backend == 'cluster':
        from robovast.service.cluster_service import ClusterService
        impl = ClusterService()
        storage = "object store"
    else:
        from robovast.service.client import LocalTransport
        impl = LocalTransport()
        storage = "local filesystem"

    click.echo(f"Starting robovast-service on http://{host}:{port} (OpenAPI at /docs)")
    click.echo(f"Backend: {backend} | storage: {storage} | Ctrl-C to stop")
    _serve(impl, host=host, port=port)


# ---------------------------------------------------------------------------
# workspace — server-side editable project inputs (thin client of the service)
# ---------------------------------------------------------------------------

_SERVICE_URL_OPTION = click.option(
    '--service-url', default='', metavar='URL',
    help='Talk to a running service at this URL (default: $ROBOVAST_SERVICE_URL, '
         'else in-process). Point at a running `vast serve` to share its workspaces.')


def _client(service_url):
    """A RobovastClient for the given URL, or $ROBOVAST_SERVICE_URL, or in-process."""
    from robovast.service.client import RobovastClient
    return RobovastClient(service_url or os.environ.get('ROBOVAST_SERVICE_URL', ''))


@cli.group()
def workspace():
    """Manage server-side workspaces (editable project inputs).

    A workspace holds a self-contained project (``.vast`` + scenario/run files) on
    the service, independent of any CWD. These commands mirror the MCP workspace
    tools and drive the same interface, so they work in-process or against a
    running ``vast serve`` (``--service-url`` / ``$ROBOVAST_SERVICE_URL``).
    """


#: Directory names skipped by ``workspace init`` — campaign outputs, not project inputs.
_INIT_EXCLUDE_DIRS = {'results'}


@workspace.command('init')
@click.argument('directory', type=click.Path(exists=True, file_okay=False))
@click.option('--name', default='', help='Workspace name (default: the directory name).')
@click.option('--exclude', 'excludes', multiple=True, metavar='NAME',
              help='Directory name to skip (repeatable). Adds to the default '
                   f"{sorted(_INIT_EXCLUDE_DIRS)}.")
@_SERVICE_URL_OPTION
def workspace_init(directory, name, excludes, service_url):
    """Create a workspace and upload every file from DIRECTORY into it.

    ``.vast``/``.osc`` are written inline; all other files go through the upload
    side channel (executability preserved). Hidden files/dirs (``.cache`` etc.) and
    output dirs (``results/``, plus any ``--exclude``) are skipped. Prints the new
    workspace id — open it in the web UI's Config tab.
    """
    from pathlib import Path
    from robovast.service.interface import (CreateUploadRequest,
                                            CreateWorkspaceRequest,
                                            WriteFileRequest)
    client = _client(service_url)
    root = Path(directory).resolve()
    skip_dirs = _INIT_EXCLUDE_DIRS | set(excludes)
    ws = client.create_workspace(CreateWorkspaceRequest(name=name or root.name))
    wid = ws.workspace_id

    count = 0
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if not path.is_file():
            continue
        # skip hidden (.cache, .robovast_*, …) and excluded output dirs (results/, …)
        if any(part.startswith('.') or part in skip_dirs for part in rel.parts):
            continue
        rel_str = rel.as_posix()
        if path.suffix.lower() in ('.vast', '.osc'):
            client.write_project_file(WriteFileRequest(
                workspace_id=wid, path=rel_str, content=path.read_text(encoding='utf-8')))
        else:
            grant = client.create_upload(CreateUploadRequest(
                workspace_id=wid, path=rel_str, executable=os.access(path, os.X_OK)))
            data = path.read_bytes()
            if grant.url:  # HTTP service issued an absolute PUT URL
                import requests
                requests.put(grant.url, data=data, timeout=120).raise_for_status()
            elif hasattr(client, 'store'):  # in-process LocalTransport
                client.store.write_upload(grant.token, data)
            else:
                raise click.ClickException(
                    f"cannot upload {rel_str!r}: this client has no upload channel")
        count += 1
        click.echo(f"  + {rel_str}")
    click.echo(f"workspace {wid} initialized from {root} ({count} files)")


@workspace.command('list')
@_SERVICE_URL_OPTION
def workspace_list(service_url):
    """List workspaces (newest first)."""
    for w in _client(service_url).list_workspaces().workspaces:
        click.echo(f"{w.workspace_id}  {w.name or '-':20}  {w.created_at or ''}")


@workspace.command('delete')
@click.argument('workspace', metavar='WORKSPACE')
@_SERVICE_URL_OPTION
def workspace_delete(workspace, service_url):
    """Delete a workspace and its inputs (existing campaigns are unaffected).

    WORKSPACE may be a ``ws-…`` id or a workspace name (unique names resolve;
    ambiguous ones must be deleted by id).
    """
    res = _client(service_url).delete_workspace(workspace)
    click.echo(res.message or ('deleted' if res.ok else 'failed'))


@cli.command()
def install_completion():
    """Install shell completion for the vast command.

    Auto-detects your shell and installs completion to the appropriate config file.

    """

    # Auto-detect shell from SHELL environment variable
    shell_env = os.environ.get('SHELL', '')
    if 'zsh' in shell_env:
        shell = 'zsh'
    elif 'fish' in shell_env:
        shell = 'fish'
    else:
        shell = 'bash'

    # Generate completion script based on shell
    script = None
    if shell == 'bash':
        script = 'eval "$(_VAST_COMPLETE=bash_source vast)"'
        config_file = os.path.expanduser('~/.bashrc')
    elif shell == 'zsh':
        script = 'eval "$(_VAST_COMPLETE=zsh_source vast)"'
        config_file = os.path.expanduser('~/.zshrc')
    elif shell == 'fish':
        script = '_VAST_COMPLETE=fish_source vast | source'
        config_file = os.path.expanduser('~/.config/fish/config.fish')
    else:
        raise click.ClickException(f"Unsupported shell for completion installation: {shell}")

    # Install to the config file
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(config_file), exist_ok=True)

        # Check if completion is already installed
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                content = f.read()
                if script in content:
                    click.echo(f"✓ Completion already installed in {config_file}")
                    return

        # Append completion script to config file
        with open(config_file, 'a') as f:
            f.write(f"\n# VAST CLI completion\n{script}\n")

        click.echo(f"✓ Completion installed successfully!")
        click.echo(f"  Shell: {shell}")
        click.echo(f"  Added to: {config_file}")
        click.echo()
        click.echo("Restart your shell or run:")
        click.echo(f"  source {config_file}")
    except Exception as e:
        click.echo(f"✗ Failed to install completion: {e}", err=True)
        raise click.Exit(1)


@cli.command()
@click.argument('archive', type=click.Path(exists=True))
@click.option('--output', '-o', default=None,
              help='Directory where results will be extracted (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force extraction even if campaign directory already exists')
def import_results(archive, output, force):
    """Import results from a downloaded archive.

    Extracts a tar.gz archive (created by ``vast execution cluster download``)
    to the results directory. This is useful for importing results that were
    downloaded on a different machine or for re-importing previously downloaded results.

    The archive should be in the format ``<campaign-name>-<timestamp>.tar.gz`` and contain
    a campaign directory with all run results.

    Requires project initialization with ``vast init`` first (unless ``--output`` is specified).
    """
    # Get output directory
    project_config = None
    if output is None:
        # Get from project configuration
        try:
            project_config = get_project_config()
            output = project_config.results_dir
        except Exception as e:
            click.echo("Error: Could not load project configuration.", err=True)
            click.echo(f"Details: {e}", err=True)
            click.echo("Use --output to specify the extraction directory.", err=True)
            sys.exit(1)

    # Validate output parameter
    if not output:
        click.echo("Error: --output parameter is required (or use 'vast init' to set default)", err=True)
        click.echo("Use --help for usage information", err=True)
        sys.exit(1)

    # Create output directory
    os.makedirs(output, exist_ok=True)

    try:
        archive_path = os.path.abspath(archive)
        click.echo(f"Importing results from: {archive_path} to results directory '{output}'...")

        # Validate the archive
        click.echo(f"Validating archive...")
        try:
            with tarfile.open(archive_path, 'r:gz') as tar:
                # Get the list of members to check structure
                members = tar.getnames()
                if not members:
                    click.echo("Error: Archive is empty", err=True)
                    sys.exit(1)

                # Extract run ID from archive contents
                top_level_dirs = set()
                for member in members:
                    parts = member.split('/')
                    if parts:
                        top_level_dirs.add(parts[0])

                if len(top_level_dirs) != 1:
                    click.echo(f"Warning: Archive contains multiple top-level directories: {top_level_dirs}")

                campaign = list(top_level_dirs)[0] if top_level_dirs else None
                if campaign and not is_campaign_dir(campaign):
                    click.echo(
                        f"Warning: Archive does not contain a recognized campaign directory "
                        f"(expected '<campaign-name>-YYYY-MM-DD-HHMMSS', found '{campaign}')")
            click.echo(f"Archive validation successful")
        except (tarfile.TarError, OSError) as e:
            click.echo(f"Error: Archive validation failed: {e}", err=True)
            sys.exit(1)

        # Check if campaign directory already exists
        if campaign:
            campaign_output_dir = os.path.join(output, campaign)
            if os.path.exists(campaign_output_dir):
                if not force:
                    click.echo(f"Error: Campaign directory already exists: {campaign_output_dir}", err=True)
                    click.echo(f"Use --force to overwrite existing run", err=True)
                    sys.exit(1)
                else:
                    click.echo(f"Removing existing campaign directory...")
                    shutil.rmtree(campaign_output_dir)

        # Extract the archive
        logger.debug(f"Extracting archive...")
        with tarfile.open(archive_path, 'r:gz') as tar:
            tar.extractall(path=output)

        logger.debug(f"Successfully extracted to: {output}")

        click.echo(f"✓ Import completed successfully!")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def load_plugins():
    """Dynamically load all VAST CLI plugins from entry points."""
    # Map of long plugin names to their short aliases
    aliases = {
        'configuration': 'config',
        'execution': 'exec',
        'evaluation': 'eval',
    }
    try:
        eps = entry_points(group='robovast.cli_plugins')

        for ep in eps:
            try:
                # Load the entry point (should return a Click group or command)
                plugin_group = ep.load()
                # Add it as a subcommand to the main CLI
                cli.add_command(plugin_group, name=ep.name)

                # Add short alias if defined
                if ep.name in aliases:
                    cli.add_command(plugin_group, name=aliases[ep.name])
            except Exception as e:
                click.echo(f"Warning: Failed to load plugin '{ep.name}': {e}", err=True)
    except Exception as e:
        click.echo(f"Warning: Failed to load plugins: {e}", err=True)


def main():
    """Main entry point for the VAST CLI."""
    # Load all plugins before running the CLI
    load_plugins()

    # Run the CLI (logging is configured via the --log-level callback)
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == '__main__':
    main()
