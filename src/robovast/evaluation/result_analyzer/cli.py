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

"""CLI for evaluation."""

import sys

import click

from robovast.common.cli import handle_cli_exception
from robovast.common.cli.project_config import ProjectConfig
from robovast.results_processing import is_postprocessing_needed, run_postprocessing


@click.group()
def evaluation():
    """Evaluate and visualize run results.

    Tools for interactive exploration and visualization of
    scenario execution results using Jupyter notebooks.
    """


def _index_campaigns(results_dir, force=False, feedback=None):
    """Ensure every batch campaign under ``results_dir`` has a campaign store.

    The results GUI reads campaigns exclusively from ``campaign.sqlite``. Search
    campaigns write their store live (and are skipped here); batch campaigns are
    indexed post-hoc from their results tree. Idempotent.
    """
    from pathlib import Path

    from robovast.common.campaign_index import build_campaign_store
    from robovast.common.execution import is_campaign_dir
    from robovast.common.store import STORE_FILENAME, CampaignStore

    root = Path(results_dir)
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not is_campaign_dir(child.name):
            continue
        # A search campaign has generation-* subdirs and writes its own store;
        # never let the batch indexer clobber it.
        if any(c.is_dir() and c.name.startswith("generation-") for c in child.iterdir()):
            continue
        store = child / STORE_FILENAME
        if store.exists():
            with CampaignStore(store) as s:
                rows = s.list_campaigns()
            if rows and rows[0]["mode"] == "search":
                continue
        try:
            build_campaign_store(child, force=force)
            if feedback:
                feedback(f"Indexed campaign: {child.name}")
        except Exception as e:  # pylint: disable=broad-except
            if feedback:
                feedback(f"Warning: could not index {child.name}: {e}")


@evaluation.command(name='index')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing run results (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Rebuild campaign stores even if they appear up to date')
def index_cmd(results_dir, force):
    """Build/refresh the ``campaign.sqlite`` store for batch campaigns.

    The results GUI reads exclusively from these stores. Search campaigns write
    their own store during execution and are skipped.
    """
    if results_dir is None:
        raw_config = ProjectConfig.load()
        if not raw_config or not raw_config.results_dir:
            raise click.ClickException(
                "Project not initialized. Run 'vast init <config-file>' first.")
        results_dir = raw_config.results_dir
    _index_campaigns(results_dir, force=force, feedback=click.echo)


@evaluation.command(name='gui')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing run results (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force postprocessing even if results directory is unchanged (bypasses caching)')
@click.option('--skip-postprocessing', is_flag=True,
              help='Skip postprocessing before launching the GUI')
@click.pass_context
def result_analyzer_cmd(ctx, results_dir, force, skip_postprocessing):
    """Launch the graphical run results gui.

    Opens a GUI application for interactive exploration and
    visualization of run results. Automatically runs postprocessing
    before launching the GUI.

    Use the global ``-V`` flag to supply a .vast file explicitly instead of the campaign copy:
    ``vast -V my.vast eval gui``

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
    vast_override = (ctx.obj or {}).get('vast_file')

    # Resolve results_dir from project config when not explicitly provided.
    # gui/postprocess never uses config_path from the project file, so only
    # results_dir is needed and config_path validation is intentionally skipped.
    if results_dir is None:
        raw_config = ProjectConfig.load()
        if not raw_config or not raw_config.results_dir:
            raise click.ClickException(
                "Project not initialized. Run 'vast init <config-file>' first."
            )
        results_dir = raw_config.results_dir

    # Run postprocessing before launching GUI (unless skipped)
    if not skip_postprocessing:
        if vast_override:
            click.echo(f"Override .vast file: {vast_override}")

        needs_pp = force or is_postprocessing_needed(results_dir, vast_file=vast_override)

        if needs_pp:
            if not click.confirm("Run postprocessing?", default=True):
                needs_pp = False

        if needs_pp:
            success, message = run_postprocessing(
                results_dir=results_dir,
                output_callback=click.echo,
                force=force,
                vast_file=vast_override,
            )

            if not success:
                click.echo(f"\n\u2717 Postprocessing failed: {message}", err=True)
                if not click.confirm(
                    "Proceed to GUI anyway?",
                    default=True
                ):
                    sys.exit(1)

    # The GUI reads campaigns exclusively from campaign.sqlite; make sure every
    # batch campaign is indexed (search campaigns already have their store).
    _index_campaigns(results_dir, force=force, feedback=click.echo)

    try:
        from PySide6.QtWidgets import \
            QApplication  # pylint: disable=import-outside-toplevel

        from .result_analyzer import \
            RunResultsAnalyzer  # pylint: disable=import-outside-toplevel
    except ImportError as e:
        click.echo(
            f"Error: Required dependencies not available: {e}\n"
            "Install result-analyzer dependencies (PySide6, matplotlib, etc.)",
            err=True
        )
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    try:
        window = RunResultsAnalyzer(base_dir=results_dir, override_vast=vast_override)
        window.show()
        exit_code = app.exec_()
        window.deleteLater()
        sys.exit(exit_code)

    except Exception as e:
        handle_cli_exception(e)


@evaluation.command(name='mcp-server')
@click.option('--transport', type=click.Choice(['stdio', 'sse', 'streamable-http']),
              default='sse', show_default=True,
              help='Transport to use.')
@click.option('--host', default='0.0.0.0', show_default=True,
              help='Host to bind when using HTTP transport.')
@click.option('--port', default=8000, show_default=True, type=int,
              help='Port to bind when using HTTP transport.')
@click.option('--debug', is_flag=True,
              help='Enable DEBUG logging for all MCP messages.')
def mcp_server_cmd(transport, host, port, debug):
    """Start the RoboVAST MCP server.

    Exposes RoboVAST tools via the Model Context Protocol so that AI
    assistants (e.g. Claude, Open WebUI) can interact with run results
    and documentation.

    Examples::

      vast eval mcp-server                                      # sse (default)
      vast eval mcp-server --transport streamable-http          # HTTP transport
    """
    from robovast.evaluation.mcp_server.server import create_server  # pylint: disable=import-outside-toplevel

    import logging  # pylint: disable=import-outside-toplevel

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.CRITICAL)

    if debug:
        # Enable only our own human-readable wrapper; keep MCP internals quiet.
        logging.getLogger("robovast.evaluation.mcp_server").setLevel(logging.DEBUG)

    mcp = create_server(host=host, port=port, debug=debug)

    try:
        if transport in ("sse", "streamable-http"):
            mcp.run(transport=transport, host=host, port=port)
        else:
            mcp.run(transport=transport)
    except KeyboardInterrupt:
        pass
