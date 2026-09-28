# Copyright (C) 2026 Frederik Pasch
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

"""``vast results build``: a campaign's tables, all of them, now.

Attached to the ``vast results`` group of ``robovast-client`` through the
``robovast.results_plugins`` entry point, so ``pip install robovast-client[data]`` has it::

    vast results build ~/Downloads/nav-through-poses-2026-09-23-11155362.tar.gz
    nav-through-poses-2026-09-23-11155362: 1180 runs, 27 workers  0%...10%...100%  138 s
    25 tables, 60.1M rows, 1.5 GB in .cache (recordings 24.0 GB)

Exits 1 when a table could not be built for some run, naming them with ``--verbose``.
"""

from __future__ import annotations

import os
import sys
import warnings
from typing import Optional

import click

from robovast_decode.tables import cache_root, read_manifest

from .data import Data, scope_of
from .engine import Scope


def _size(path: str, skip: Optional[str] = None) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        if skip is not None:
            dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) != skip]
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _human(n: float, unit: str = "") -> str:
    """``60.1M``, ``1.5 GB``: three significant figures and a decimal prefix."""
    for prefix in ("", "k", "M", "G", "T"):
        if abs(n) < 1000 or prefix == "T":
            number = f"{n:.0f}" if not prefix else f"{n:.1f}"
            return f"{number}{' ' if unit else ''}{prefix}{unit}"
        n /= 1000
    raise AssertionError("unreachable")


def _summary(campaign_dir: str) -> str:
    manifest = read_manifest(campaign_dir)
    tables = [t for t in manifest.get("tables", {}) if not t.startswith("_")]
    rows = 0
    for entry in manifest.get("tables", {}).values():
        whole = entry.get("campaign") or {}
        rows += whole.get("rows") or 0
        rows += sum(run.get("rows") or 0 for run in entry.get("runs", {}).values()
                    if not run.get("compacted"))
    cache = cache_root(campaign_dir)
    return (f"{len(tables)} tables, {_human(rows)} rows, "
            f"{_human(_size(os.path.join(cache, 'tables')), 'B')} in .cache "
            f"(recordings {_human(_size(campaign_dir, skip=cache), 'B')})")


@click.command(name="build")
@click.argument("campaign", type=click.Path(exists=True))
@click.option("--table", "tables", multiple=True, metavar="NAME",
              help="A table to build; repeatable (default: every table).")
@click.option("--jobs", type=click.IntRange(min=1), default=None,
              help="Runs built at once (default: every core but one).")
@click.option("--no-compact", is_flag=True,
              help="Keep one file per run instead of one file per table.")
@click.option("--quiet", is_flag=True, help="No progress line.")
@click.option("--verbose", is_flag=True,
              help="Name every table that could not be built for some run.")
def build(campaign, tables, jobs, no_compact, quiet, verbose):
    """Build a campaign's tables now, and compact them into one file each.

    CAMPAIGN is a campaign directory or its downloaded archive, which is extracted beside
    it first. A query builds what it names on first use either way; this builds every table
    ahead, with every core but one, and merges each table's run files into one file, which
    is smaller and faster to read. Exits 1 when a table could not be built for some run.
    """
    scope = scope_of(os.path.expanduser(campaign))
    data = Data([Scope(scope.campaign_dir)])
    with warnings.catch_warnings():
        # The problems are reported below, once, in the command's own words.
        warnings.simplefilter("ignore")
        built = data.build(list(tables) or None, workers=jobs, compact=not no_compact,
                           progress=not quiet)
    click.echo(_summary(scope.campaign_dir))
    for report in built.compacted.values():
        for table, why in sorted(report.skipped.items()):
            click.echo(f"not compacted: {table}: {why}", err=True)
    if built.problems:
        names = sorted({p.table for p in built.problems})
        click.echo(f"{len(built.problems)} table(s) could not be built for some run: "
                   f"{', '.join(names)}", err=True)
        for problem in built.problems if verbose else []:
            click.echo(f"  {problem}", err=True)
        sys.exit(1)


__all__ = ["build"]
