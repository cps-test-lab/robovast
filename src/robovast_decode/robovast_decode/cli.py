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

"""``robovast-decode``: build a campaign directory's tables from its recordings, offline."""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from .build import available_tables, build


def _config(value):
    if value is None:
        return None
    if value.startswith("@"):
        with open(value[1:], encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="robovast-decode",
        description="Build the tables of a campaign directory from its recordings. Tables are "
                    "parquet files under <campaign>/.cache/, cataloged by .cache/MANIFEST.json; "
                    "one already built from the same recording is not built again.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build tables (all of them, or those named)")
    b.add_argument("campaign_dir")
    b.add_argument("--table", action="append", dest="tables", metavar="NAME",
                   help="a table to build; repeatable (default: every table)")
    b.add_argument("--run", action="append", dest="runs", metavar="CONFIG/RUN",
                   help="a run to build for; repeatable (default: every run)")
    b.add_argument("--config", help='decoder configuration as JSON, or @file: '
                                    '{"groups": [{"bag_dir": ..., "plugins": [...]}]}')
    b.add_argument("--force", action="store_true", help="rebuild tables already current")
    t = sub.add_parser("tables", help="list the tables the recordings can give, and what is built")
    t.add_argument("campaign_dir")
    t.add_argument("--config")
    args = parser.parse_args(argv)

    if args.command == "tables":
        for name, counts in sorted(available_tables(args.campaign_dir, _config(args.config)).items()):
            print(f"{name:45s} built for {counts['built']} of {counts['runs']} recordings")
        return 0

    started = time.perf_counter()
    report = build(args.campaign_dir, tables=args.tables, runs=args.runs,
                   config=_config(args.config), force=args.force)
    for table, runs in sorted(report.built.items()):
        print(f"built   {table}: {len(runs)} run(s)")
    for table, runs in sorted(report.skipped.items()):
        print(f"current {table}: {len(runs)} run(s)")
    for table, by_run in sorted(report.failed.items()):
        for run, reason in sorted(by_run.items()):
            print(f"FAILED  {table} for {run}: {reason}", file=sys.stderr)
    for table in report.unknown:
        print(f"UNKNOWN {table}: no recording of this campaign gives it", file=sys.stderr)
    print(f"{time.perf_counter() - started:.1f}s")
    return 1 if (report.failed or report.unknown) else 0


if __name__ == "__main__":
    sys.exit(main())
