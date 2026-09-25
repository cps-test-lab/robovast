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

"""A campaign's data as pandas: a directory, an archive or a set of campaigns in, frames out.

The same object answers for a whole campaign, one configuration or one run, and which one a
path is decides it: :func:`open_data` walks up from the path to the campaign (the directory
holding ``campaign.db``) and scopes everything below to the node the path names -- which is
how a notebook cell reads the same on a laptop and in the Results Explorer, where ``DATA_DIR``
is the selected node's directory.

Every table is built on first use, into the campaign's own ``.cache/``, by the decoder the
service uses (:mod:`robovast_data.engine`); the second use reads it.
"""

from __future__ import annotations

import glob
import os
import tarfile
import warnings
from typing import Dict, Iterable, List, Optional

import pandas as pd
import yaml

from robovast_decode.layout import STORE
from robovast_decode.runs import RUNS_TABLE

from .engine import Engine, Problem, Scope
from .statement import QueryError

#: Above this many rows, :meth:`Data.table` says what it is about to hold before it does.
LARGE_TABLE_ROWS = 5_000_000

_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar")
_GLOB_CHARS = set("*?[")


def _campaign_root(path: str) -> str:
    """The campaign directory at or above *path*: the one holding ``campaign.db``."""
    current = os.path.abspath(path)
    while True:
        if os.path.isfile(os.path.join(current, STORE)):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            raise FileNotFoundError(f"{path} is not inside a campaign directory (no {STORE} "
                                    "in it or above it)")
        current = parent


def _extracted(archive: str) -> str:
    """The campaign a downloaded archive holds, extracted beside it on first use."""
    base = os.path.basename(archive)
    for suffix in _ARCHIVE_SUFFIXES:
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    target = os.path.join(os.path.dirname(os.path.abspath(archive)), base)
    marker = os.path.join(target, ".extracted")
    if not os.path.isfile(marker):
        os.makedirs(target, exist_ok=True)
        with tarfile.open(archive) as tar:
            tar.extractall(target, filter="data")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(os.path.abspath(archive) + "\n")
    entries = [e for e in os.listdir(target) if not e.startswith(".")]
    if not os.path.isfile(os.path.join(target, STORE)) and len(entries) == 1:
        return os.path.join(target, entries[0])
    return target


def scope_of(path: str) -> Scope:
    """The campaign, configuration or run *path* names.

    A path below a run (its ``rosbag2/`` directory, say) is that run; a path under a
    campaign's own directories (``_config``, ``_jobs``, ...) is the campaign.
    """
    if os.path.isfile(path) and path.endswith(_ARCHIVE_SUFFIXES):
        path = _extracted(path)
    root = _campaign_root(path)
    parts = [p for p in os.path.relpath(os.path.abspath(path), root).split(os.sep)
             if p not in (".", "")]
    if not parts or parts[0].startswith(("_", ".")):
        return Scope(root)
    if len(parts) == 1 or not parts[1].isdigit():
        return Scope(root, parts[0])
    return Scope(root, parts[0], int(parts[1]))


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class ConfigFiles:
    """A configuration's resolved files, as its runs received them."""

    def __init__(self, campaign_dir: str, name: str):
        self.name = name
        self.path = os.path.join(campaign_dir, name, "_config")
        if not os.path.isdir(self.path):
            raise KeyError(f"no configuration {name!r} in {campaign_dir}")

    def text(self, relpath: str) -> str:
        with open(os.path.join(self.path, relpath), encoding="utf-8") as fh:
            return fh.read()

    def yaml(self, relpath: str):
        return yaml.safe_load(self.text(relpath))

    def files(self) -> List[str]:
        return sorted(os.path.relpath(os.path.join(root, f), self.path)
                      for root, _dirs, names in os.walk(self.path) for f in names)


class Reader:
    """``table()`` over whatever answers ``_frame(sql)``: a campaign on disk or on a service."""

    def _frame(self, sql: str, params=None) -> pd.DataFrame:
        raise NotImplementedError

    def table(self, name: str, config: Optional[str] = None, run: Optional[int] = None,
              with_params: bool = False, columns: Optional[List[str]] = None) -> pd.DataFrame:
        """Table *name* for the scope (narrowed to *config* and *run*), as a DataFrame.

        *with_params* adds the runs' ``param_*`` columns; *columns* selects some columns only.
        """
        selected = ", ".join(f"t.{_ident(c)}" for c in columns) if columns else "t.*"
        where = []
        if config is not None:
            where.append(f"t.config_name = {_quote(config)}")
        if run is not None:
            where.append(f"t.run_id = {int(run)}")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        if with_params:
            params = [c for c in self._frame(f"SELECT * FROM {RUNS_TABLE} LIMIT 0").columns
                      if c.startswith("param_")]
            added = "".join(f", r.{_ident(c)}" for c in params)
            sql = (f"SELECT {selected}{added} "
                   f"FROM {_ident(name)} t LEFT JOIN {RUNS_TABLE} r "
                   "ON r.campaign_id = t.campaign_id AND r.config_name = t.config_name "
                   f"AND r.run_id = t.run_id{clause}")
        else:
            sql = f"SELECT {selected} FROM {_ident(name)} t{clause}"
        if config is None and run is None:
            count = self._frame(f"SELECT count(*) AS n FROM {_ident(name)}")["n"].iloc[0]
            if count > LARGE_TABLE_ROWS:
                warnings.warn(
                    f"{name} holds {count:,} rows here; reading them all into one DataFrame "
                    "takes the memory of all of them. Narrow it with config= and run=, or "
                    "aggregate with sql().", stacklevel=2)
        return self._frame(sql)


class Data(Reader):
    """What one or more scopes hold, as DataFrames. Build one with :func:`open_data`,
    :class:`Campaign` or :class:`Corpus`."""

    def __init__(self, scopes: Iterable[Scope], **engine_options):
        self.scopes = list(scopes)
        self.engine = Engine(self.scopes, **engine_options)

    def __repr__(self) -> str:
        names = ", ".join(_describe(s) for s in self.scopes[:3])
        more = f", ... {len(self.scopes) - 3} more" if len(self.scopes) > 3 else ""
        return f"<{type(self).__name__} {names}{more}>"

    def _frame(self, sql: str, params=None) -> pd.DataFrame:
        with self.engine.execute(sql, params) as (con, problems):
            frame = con.df()
        _report(problems)
        return frame

    @property
    def runs(self) -> pd.DataFrame:
        """One row per run in scope (and per unit that produced none): outcome, host,
        ``param_*`` per varied factor."""
        return self._frame(f"SELECT * FROM {RUNS_TABLE} "
                           "ORDER BY campaign_id, config_name, run_id NULLS LAST")

    @property
    def tables(self) -> pd.DataFrame:
        """What can be read here: every table, how many runs it is built for, and the views."""
        rows = [{"name": name, "kind": entry["kind"], "runs": entry["runs"],
                 "built": entry["built"], "failed": len(entry["failed"]),
                 "columns": None if entry["columns"] is None else len(entry["columns"])}
                for name, entry in sorted(self.engine.catalog().items())]
        return pd.DataFrame(rows)

    def sql(self, query: str, params=None) -> pd.DataFrame:
        """Any ``SELECT`` over the tables and views here, as a DataFrame."""
        return self._frame(query, params)

    def config(self, name: str) -> ConfigFiles:
        """The resolved files configuration *name* ran with."""
        campaigns = {s.campaign_dir for s in self.scopes}
        if len(campaigns) != 1:
            raise ValueError("config() reads one campaign's configuration; use it on a "
                             "Campaign")
        return ConfigFiles(campaigns.pop(), name)


def _describe(scope: Scope) -> str:
    text = scope.campaign_id
    if scope.config_name is not None:
        text += f"/{scope.config_name}"
    if scope.run_id is not None:
        text += f"/{scope.run_id}"
    return text


def _report(problems: List[Problem]) -> None:
    if problems:
        shown = "\n  ".join(str(p) for p in problems[:10])
        more = f"\n  ... {len(problems) - 10} more" if len(problems) > 10 else ""
        warnings.warn(f"{len(problems)} table(s) could not be built for some runs; the "
                      f"answer leaves them out:\n  {shown}{more}", stacklevel=3)


def open_data(path: str, **options):
    """The data a path selects: a campaign, one of its configurations, or one run.

    *path* is a campaign directory, any directory inside one, a downloaded ``.tar.gz``
    (extracted beside it on first use), or a campaign on a service,
    ``https://<service>/campaigns/<campaign_id>`` (pass ``token=``; see
    :mod:`robovast_data.remote`).
    """
    from .remote import RemoteCampaign, is_url  # pylint: disable=import-outside-toplevel
    if is_url(path):
        return RemoteCampaign(path, **options)
    return Data([scope_of(os.path.expanduser(path))], **options)


class Campaign(Data):
    """A whole campaign, from its directory, any directory inside it, or its archive -- or
    from a service, ``https://<service>/campaigns/<campaign_id>`` with ``token=``, which
    gives a :class:`~robovast_data.remote.RemoteCampaign` answering the same calls."""

    def __new__(cls, path: str, **options):
        from .remote import RemoteCampaign, is_url  # pylint: disable=import-outside-toplevel
        if is_url(path):
            return RemoteCampaign(path, **options)
        return super().__new__(cls)

    def __init__(self, path: str, **engine_options):
        scope = scope_of(os.path.expanduser(path))
        super().__init__([Scope(scope.campaign_dir)], **engine_options)


class Corpus(Data):
    """Several campaigns as one: every table carries ``campaign_id``.

    *paths* is a glob, or a list of campaign directories or archives.
    """

    def __init__(self, paths, **engine_options):
        if isinstance(paths, str):
            paths = sorted(glob.glob(os.path.expanduser(paths)))
        from .remote import is_url  # pylint: disable=import-outside-toplevel
        remote = [p for p in paths if is_url(p)]
        if remote:
            raise ValueError(f"a Corpus reads campaign directories and archives; {remote[0]} "
                             "is on a service -- open it with Campaign(url) instead")
        scopes: Dict[str, Scope] = {}
        for path in paths:
            scope = scope_of(os.path.expanduser(path))
            scopes.setdefault(scope.campaign_dir, Scope(scope.campaign_dir))
        if not scopes:
            raise FileNotFoundError(f"no campaign matches {paths!r}")
        super().__init__(scopes.values(), **engine_options)


def _data(path, token: Optional[str] = None):
    if isinstance(path, str) and _GLOB_CHARS & set(path):
        return Corpus(path)
    return open_data(path, token=token) if token is not None else open_data(path)


def read_table(path, name: str, token: Optional[str] = None, **options) -> pd.DataFrame:
    """Table *name* of what *path* selects (a glob: of every campaign it matches; a
    service URL: that campaign, with *token*)."""
    return _data(path, token).table(name, **options)


def read_runs(path, token: Optional[str] = None) -> pd.DataFrame:
    """The ``runs`` of what *path* selects (a glob: of every campaign it matches; a service
    URL: that campaign, with *token*)."""
    return _data(path, token).runs


__all__ = ["Campaign", "ConfigFiles", "Corpus", "Data", "LARGE_TABLE_ROWS", "QueryError", "Reader",
           "open_data", "read_runs", "read_table", "scope_of"]
