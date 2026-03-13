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


@evaluation.command(name='gui')
@click.option('--results-dir', '-r', default=None,
              help='Directory containing run results (uses project results dir if not specified)')
@click.option('--force', '-f', is_flag=True,
              help='Force postprocessing even if results directory is unchanged (bypasses caching)')
@click.option('--skip-postprocessing', is_flag=True,
              help='Skip postprocessing before launching the GUI')
@click.option('--override', '-o', default=None, metavar='VAST_FILE',
              help='Override the .vast file used for postprocessing instead of the one '
                   'found in campaign-<id>/_config/')
def result_analyzer_cmd(results_dir, force, skip_postprocessing, override):
    """Launch the graphical run results analyzer.

    Opens a GUI application for interactive exploration and
    visualization of run results. Automatically runs postprocessing
    before launching the GUI.

    Use --override to supply a .vast file explicitly instead of the campaign copy.

    Requires project initialization with ``vast init`` first (unless ``--results-dir`` is specified).
    """
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
        if override:
            click.echo(f"Override .vast file: {override}")

        needs_pp = force or is_postprocessing_needed(results_dir, vast_file=override)

        if needs_pp:
            if not click.confirm("Run postprocessing?", default=True):
                needs_pp = False

        if needs_pp:
            success, message = run_postprocessing(
                results_dir=results_dir,
                output_callback=click.echo,
                force=force,
                vast_file=override,
            )

            if not success:
                click.echo(f"\n\u2717 Postprocessing failed: {message}", err=True)
                if not click.confirm(
                    "Proceed to GUI anyway?",
                    default=True
                ):
                    sys.exit(1)

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
        window = RunResultsAnalyzer(base_dir=results_dir, override_vast=override)
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
