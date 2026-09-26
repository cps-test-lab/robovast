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

"""A campaign's export: its tables as files, its records and its recordings, in one tarball.

An export is built for one request (:class:`~robovast.service.interface.ExportRequest`) under
the campaign's ``.cache/exports/<export_id>/``: rebuildable, disposable, and counted and
cleared with the table cache. What it holds is decided by the request:

* **tables** -- the campaign's logical tables, one file per table, written by DuckDB from the
  same views a query reads (``COPY (SELECT * FROM <table>) TO ...``) after the engine built
  them for every run. ``runs`` is always written. Parquet is the tables as they are; CSV is
  the same rows as text.
* **bags** -- each run's ``rosbag2/`` and ``roqsim_bag/`` and each job's ``logs/rosout_bag/``,
  copied as recorded (``mcap``), or with every rosbag2 bag rewritten in rosbag2's sqlite3
  storage from the mcap records (``sqlite3``): the raw CDR bytes are written through
  ``rosbags``, with each topic's definition taken from the recording, its sidecar or the
  distro's types. A channel whose definition none of the three carry is refused, naming the
  topic and the type, rather than left out. roqsim's recording is not a rosbag2 and is
  copied as it is.
* **records** -- everything the archive carries except the recordings and ``.cache``:
  ``campaign.db``, ``_config/``, ``_execution/``, ``_transient/``, the metadata documents and
  every run's own files.

The tarball holds ``export.json`` (the request, the decoder version, the row count and file
of every table, the campaign id and when it was made), ``tables/`` and the campaign tree
under ``<campaign_id>/``. The same ``export.json`` is left beside the tarball, and is how a
status read after a service restart still answers; a failed export leaves ``error.json``
instead. The export builds on a thread of its own, so several exports of one campaign may
build at once, and while any does the campaign's tables are in use.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

from robovast.common.query_limits import query_limits
from robovast.results_processing.campaign_tables import exports_root
from robovast.service.interface import ExportRef, ExportRequest, ExportStatus, Routes

logger = logging.getLogger(__name__)

#: Written when an export starts: the request and when. Its presence without either file
#: below is an export that was building when the service stopped.
REQUEST_FILE = "request.json"
#: Written when an export is done: what it holds. Also the tarball's first member.
EXPORT_FILE = "export.json"
#: Written when an export failed: why.
ERROR_FILE = "error.json"
#: Under the tarball's root: one file per table.
TABLES_MEMBER = "tables"

#: An export id: twelve hex characters, minted here and never taken from a path unchecked.
_EXPORT_ID = re.compile(r"^[0-9a-f]{12}$")

#: Directory names under a run or a job that hold a recording.
_ROSBAG2_DIR = re.compile(r"^rosbag2(?:_\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})?$")
_ROQSIM_BAG = "roqsim_bag"
_ROSOUT_BAG = "rosout_bag"

#: Read size for the download generator.
_CHUNK = 1024 * 1024

#: The rosbag2 metadata version the rewritten bags are written in.
_ROSBAG2_VERSION = 9


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def export_dir(campaign_dir, export_id: str) -> Path:
    """The directory of one export. ``KeyError`` for an id that is not one."""
    if not _EXPORT_ID.match(export_id):
        raise KeyError(f"no export {export_id!r}")
    return exports_root(campaign_dir) / export_id


def export_file_name(campaign_id: str, export_id: str) -> str:
    """The name the export's tarball is written and served under."""
    return f"{campaign_id}-export-{export_id}.tar.gz"


# -- what is on disk --------------------------------------------------------------------------

def read_status(campaign_dir, export_id: str) -> ExportStatus:
    """An export's status from its directory alone.

    Done when ``export.json`` is there, failed when ``error.json`` is; an export with only
    its request on disk was building when the process that built it stopped, and reads as
    failed for that reason. ``KeyError`` for an id the campaign has no directory for.
    """
    path = export_dir(campaign_dir, export_id)
    if not path.is_dir():
        raise KeyError(f"no export {export_id!r} of this campaign")
    if (path / EXPORT_FILE).is_file():
        with open(path / EXPORT_FILE, encoding="utf-8") as fh:
            manifest = json.load(fh)
        return ExportStatus(export_id=export_id, done=True, bytes=manifest.get("bytes", 0),
                            tables={name: entry["rows"]
                                    for name, entry in manifest.get("tables", {}).items()},
                            started_at=manifest.get("started_at"),
                            finished_at=manifest.get("created_at"))
    started = None
    if (path / REQUEST_FILE).is_file():
        with open(path / REQUEST_FILE, encoding="utf-8") as fh:
            started = json.load(fh).get("started_at")
    if (path / ERROR_FILE).is_file():
        with open(path / ERROR_FILE, encoding="utf-8") as fh:
            failure = json.load(fh)
        return ExportStatus(export_id=export_id, done=True, error=failure.get("error", ""),
                            started_at=started, finished_at=failure.get("finished_at"))
    return ExportStatus(export_id=export_id, done=True, started_at=started,
                        error="the export was still building when the service stopped; "
                              "start it again")


def export_file(campaign_dir, campaign_id: str, export_id: str) -> Path:
    """The finished tarball. ``KeyError`` until it is done, ``RuntimeError`` once it failed.

    Decided from the export's files alone, so the data plane answers it without knowing
    whether the process building the export is still at it: an export with neither its
    manifest nor its error on disk is not done, whatever became of its builder.
    """
    path = export_dir(campaign_dir, export_id)
    if not path.is_dir():
        raise KeyError(f"no export {export_id!r} of {campaign_id}")
    if (path / ERROR_FILE).is_file():
        with open(path / ERROR_FILE, encoding="utf-8") as fh:
            reason = json.load(fh).get("error", "")
        raise RuntimeError(f"export {export_id} of {campaign_id} failed: {reason}")
    file = path / export_file_name(campaign_id, export_id)
    if not (path / EXPORT_FILE).is_file() or not file.is_file():
        raise KeyError(f"export {export_id} of {campaign_id} is not done yet")
    return file


def iter_file(path, chunk_size: int = _CHUNK) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                return
            yield block


# -- the tables ---------------------------------------------------------------------------------

def plan_tables(catalog: Dict[str, dict], requested: Optional[List[str]]) -> List[str]:
    """The tables an export writes, checked against the campaign's *catalog*.

    *requested* ``None`` is every table of the catalog that is built from the campaign's
    records or its record (``kind == "table"``); a list is those names, each of which the
    catalog must have -- a view or a record table may be named. ``runs`` is always first.
    """
    if requested is None:
        names = [name for name, entry in catalog.items() if entry["kind"] == "table"]
    else:
        unknown = sorted(set(requested) - set(catalog))
        if unknown:
            raise ValueError(f"no table {', '.join(repr(n) for n in unknown)} in this "
                             f"campaign; it has: {', '.join(sorted(catalog))}")
        names = list(dict.fromkeys(requested))
    names = [n for n in names if n != "runs"]
    return ["runs"] + sorted(names)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _qualified(name: str) -> str:
    schema, _, table = name.rpartition(".")
    return f"{_quote(schema)}.{_quote(table)}" if schema else _quote(table)


def write_tables(engine, tables: List[str], fmt: str, out_dir: Path,
                 written: Callable[[str, int, str], None]) -> None:
    """Build *tables* for every run in *engine*'s scope, then write each to *out_dir*.

    One ``COPY`` per table from the connection the engine defines for them, into
    *out_dir*, the one directory outside the table cache that connection may write to.
    *written* is called with the table's name, its row count and its file as each lands.
    """
    problems = engine.ensure(engine.tables_for(tables))
    for problem in problems[:50]:
        logger.warning("  %s", problem)
    if len(problems) > 50:
        logger.warning("  ... %d more", len(problems) - 50)
    out_dir.mkdir(parents=True, exist_ok=True)
    extension = "parquet" if fmt == "parquet" else "csv"
    options = ("FORMAT PARQUET, COMPRESSION ZSTD" if fmt == "parquet"
               else "FORMAT CSV, HEADER")
    con = engine.connect(tables, writable=str(out_dir))
    try:
        for table in tables:
            file = f"{table}.{extension}"
            target = str(out_dir / file).replace("'", "''")
            rows = con.execute(f"COPY (SELECT * FROM {_qualified(table)}) TO '{target}' "
                               f"({options})").fetchone()[0]
            written(table, int(rows), file)
    finally:
        con.close()


# -- the recordings -----------------------------------------------------------------------------

@dataclass
class _Bag:
    """One recording of the campaign: where it is, and where it goes in the export."""
    path: Path
    rel: str
    rosbag2: bool


def campaign_bags(campaign_dir) -> List[_Bag]:
    """Every recording the export ships, campaign-relative, each job's once."""
    from robovast_decode.build import (  # pylint: disable=import-outside-toplevel
        find_runs, roqsim_recording, scenario_recording)
    root = Path(campaign_dir)
    bags: Dict[str, _Bag] = {}

    def add(path, rosbag2):
        if path is None:
            return
        rel = os.path.relpath(path, root)
        bags.setdefault(rel, _Bag(Path(path), rel, rosbag2))

    for run in find_runs(str(root)):
        add(scenario_recording(run), True)
        add(roqsim_recording(run), False)
        if run.job_dir:
            rosout = os.path.join(run.job_dir, "logs", _ROSOUT_BAG)
            if os.path.isdir(rosout):
                add(rosout, True)
    return [bags[k] for k in sorted(bags)]


def rewrite_sqlite3(bag_dir: Path, out_dir: Path) -> int:
    """Write the rosbag2 bag at *bag_dir* into *out_dir* in sqlite3 storage; the messages.

    Each channel's connection carries the definition the recording holds for its type, or
    the sidecar's, or the distro's; a type none of them defines is refused by topic and
    type, since a bag missing a topic would read as a recording that never had it.
    """
    from rosbags.rosbag2 import StoragePlugin, Writer  # pylint: disable=import-outside-toplevel

    from robovast_decode.decode import segments  # pylint: disable=import-outside-toplevel
    from robovast_decode.definitions import (  # pylint: disable=import-outside-toplevel
        SIDECAR_NAME, catalog_for)
    from robovast_decode.framing import (  # pylint: disable=import-outside-toplevel
        McapTail, Message)

    files = segments(str(bag_dir))
    if not files:
        raise RuntimeError(f"{bag_dir} holds no mcap file to rewrite")
    sidecar = {}
    if (bag_dir / SIDECAR_NAME).is_file():
        with open(bag_dir / SIDECAR_NAME, encoding="utf-8") as fh:
            sidecar = json.load(fh)
    count = 0
    with Writer(out_dir, version=_ROSBAG2_VERSION, storage_plugin=StoragePlugin.SQLITE3) as w:
        connections: Dict[str, object] = {}
        for file in files:
            tail = McapTail(file)
            catalog = None
            for record in tail.read():
                if not isinstance(record, Message):
                    continue
                channel = tail.channels[record.channel_id]
                connection = connections.get(channel.topic)
                if connection is None:
                    if catalog is None:
                        catalog = catalog_for(tail.schemas.values(), str(bag_dir))
                    schema = tail.schemas[channel.schema_id]
                    if not catalog.ensure(schema.name, channel.message_encoding):
                        raise RuntimeError(
                            f"{bag_dir}: topic {channel.topic} of type {schema.name} has no "
                            f"message definition in the recording, its sidecar or the "
                            f"distro's types, so it cannot be written as sqlite3")
                    text = schema.data.decode("utf-8") if schema.data else sidecar.get(schema.name)
                    if not text:
                        text, _ = catalog.store.generate_msgdef(schema.name, ros_version=2)
                    connection = connections[channel.topic] = w.add_connection(
                        channel.topic, schema.name, msgdef=text,
                        rihs01=catalog.store.hash_rihs01(schema.name),
                        serialization_format=channel.message_encoding)
                w.write(connection, record.log_time, record.data)
                count += 1
    if sidecar:
        shutil.copy2(bag_dir / SIDECAR_NAME, out_dir / SIDECAR_NAME)
    return count


# -- the tarball ------------------------------------------------------------------------------

def _is_bag_dir(name: str) -> bool:
    return bool(_ROSBAG2_DIR.match(name)) or name in (_ROQSIM_BAG, _ROSOUT_BAG)


def add_records(tar: tarfile.TarFile, campaign_dir, campaign_id: str) -> int:
    """Add the campaign's records under ``<campaign_id>/``; how many files.

    Everything but the recordings and ``.cache``. Symlinks -- the ``job`` links beside the
    runs -- are symlink members, as the archive carries them.
    """
    from robovast_decode.tables import CACHE_DIR  # pylint: disable=import-outside-toplevel
    root = Path(campaign_dir)
    count = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        rel = os.path.relpath(current, root)
        links = [n for n in sorted(os.listdir(current)) if os.path.islink(os.path.join(current, n))]
        dirs[:] = sorted(d for d in dirs if d not in links
                         and not (rel == "." and d == CACHE_DIR) and not _is_bag_dir(d))
        for name in [f for f in sorted(files) if f not in links] + links:
            path = os.path.join(current, name)
            arc = f"{campaign_id}/{name}" if rel == "." else f"{campaign_id}/{rel}/{name}"
            tar.add(path, arcname=arc, recursive=False)
            count += 1
    return count


def add_tree(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    tar.add(str(path), arcname=arcname, recursive=True)


def _add_bytes(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, io.BytesIO(payload))


# -- the build ----------------------------------------------------------------------------------

@dataclass
class _Running:
    status: ExportStatus
    campaign_id: str
    thread: Optional[threading.Thread] = None
    tables: Dict[str, dict] = field(default_factory=dict)


class ExportStore:
    """The exports this process is building, and the status of every export on disk.

    In memory only while an export builds; once it is done its directory says everything a
    status read needs, so a restart forgets nothing that finished.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._running: Dict[tuple, _Running] = {}

    def running(self, campaign_id: str) -> bool:
        """Whether an export of *campaign_id* is building now."""
        with self._lock:
            return any(key[0] == campaign_id for key in self._running)

    def status(self, campaign_dir, campaign_id: str, export_id: str) -> ExportStatus:
        with self._lock:
            live = self._running.get((campaign_id, export_id))
            if live is not None:
                return live.status.model_copy(deep=True)
        return read_status(campaign_dir, export_id)

    def start(self, campaign_dir, campaign_id: str, request: ExportRequest,
              tables: List[str]) -> ExportRef:
        """Start building an export of *campaign_dir* as *request* asks; returns its handle.

        *tables* is the plan :func:`plan_tables` made from the request, so what is written
        was checked against the catalog before this is called.
        """
        export_id = secrets.token_hex(6)
        path = export_dir(campaign_dir, export_id)
        path.mkdir(parents=True, exist_ok=False)
        started_at = _now()
        with open(path / REQUEST_FILE, "w", encoding="utf-8") as fh:
            json.dump({"export_id": export_id, "campaign_id": campaign_id,
                       "request": request.model_dump(), "tables": tables,
                       "started_at": started_at}, fh, indent=2)
        entry = _Running(status=ExportStatus(export_id=export_id, started_at=started_at),
                         campaign_id=campaign_id)
        with self._lock:
            self._running[(campaign_id, export_id)] = entry
        entry.thread = threading.Thread(
            target=self._build, name=f"export-{export_id}", daemon=True,
            args=(Path(campaign_dir), campaign_id, export_id, request, tables, entry))
        entry.thread.start()
        return ExportRef(export_id=export_id,
                         url=Routes.campaign_export_download(campaign_id, export_id))

    def wait(self, campaign_id: str, export_id: str, timeout: Optional[float] = None) -> None:
        """Block until the export is over; for a caller in the same process."""
        with self._lock:
            live = self._running.get((campaign_id, export_id))
        if live is not None and live.thread is not None:
            live.thread.join(timeout)

    def _build(self, campaign_dir: Path, campaign_id: str, export_id: str,
               request: ExportRequest, tables: List[str], entry: _Running) -> None:
        path = export_dir(campaign_dir, export_id)
        try:
            manifest = build_export(campaign_dir, campaign_id, export_id, request, tables,
                                    on_table=lambda name, rows, file: self._table_done(
                                        entry, name, rows, file))
            with self._lock:
                entry.status.done = True
                entry.status.bytes = manifest["bytes"]
                entry.status.finished_at = manifest["created_at"]
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Export %s of %s failed", export_id, campaign_id)
            finished = _now()
            with open(path / ERROR_FILE, "w", encoding="utf-8") as fh:
                json.dump({"error": str(exc), "finished_at": finished}, fh, indent=2)
            with self._lock:
                entry.status.done = True
                entry.status.error = str(exc)
                entry.status.finished_at = finished
        finally:
            with self._lock:
                self._running.pop((campaign_id, export_id), None)

    def _table_done(self, entry: _Running, name: str, rows: int, file: str) -> None:
        with self._lock:
            entry.status.tables[name] = rows
            entry.tables[name] = {"rows": rows, "file": file}


def build_export(campaign_dir: Path, campaign_id: str, export_id: str, request: ExportRequest,
                 tables: List[str], on_table: Callable[[str, int, str], None]) -> dict:
    """Build one export in its directory; the ``export.json`` it wrote.

    The tables are written first, then the tarball is assembled in one pass -- the manifest,
    the table files, the records, the recordings -- and the scratch beside it removed, so
    what stays under the export's directory is the tarball and its manifest.
    """
    from robovast_data import Engine, Scope  # pylint: disable=import-outside-toplevel
    from robovast_decode import DATA_CONTRACT  # pylint: disable=import-outside-toplevel
    from robovast_decode import __version__ as decoder_version  # pylint: disable=import-outside-toplevel

    path = export_dir(campaign_dir, export_id)
    started_at = None
    with open(path / REQUEST_FILE, encoding="utf-8") as fh:
        started_at = json.load(fh).get("started_at")
    scratch = path / "scratch"
    shutil.rmtree(scratch, ignore_errors=True)
    written: Dict[str, dict] = {}

    def table_written(name, rows, file):
        written[name] = {"rows": rows, "file": f"{TABLES_MEMBER}/{file}"}
        on_table(name, rows, file)

    write_tables(Engine([Scope(str(campaign_dir))], **query_limits()), tables, request.format,
                 scratch / TABLES_MEMBER, table_written)
    bags = campaign_bags(campaign_dir) if request.bags != "none" else []
    if request.bags == "sqlite3":
        for bag in bags:
            if bag.rosbag2:
                out = scratch / "bags" / bag.rel
                out.parent.mkdir(parents=True, exist_ok=True)
                rewrite_sqlite3(bag.path, out)
    manifest = {
        "campaign_id": campaign_id,
        "export_id": export_id,
        "request": request.model_dump(),
        "decoder": decoder_version,
        "data_contract": DATA_CONTRACT,
        "tables": written,
        "bags": [b.rel for b in bags],
        "records": request.records,
        "started_at": started_at,
        "created_at": _now(),
    }
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    name = export_file_name(campaign_id, export_id)
    tmp = path / (name + ".incoming")
    with tarfile.open(tmp, "w:gz") as tar:
        _add_bytes(tar, EXPORT_FILE, payload)
        for entry in written.values():
            tar.add(str(scratch / entry["file"]), arcname=entry["file"], recursive=False)
        if request.records:
            add_records(tar, campaign_dir, campaign_id)
        for bag in bags:
            source = (scratch / "bags" / bag.rel if request.bags == "sqlite3" and bag.rosbag2
                      else bag.path)
            add_tree(tar, source, f"{campaign_id}/{bag.rel}")
    os.replace(tmp, path / name)
    shutil.rmtree(scratch, ignore_errors=True)
    manifest["bytes"] = os.path.getsize(path / name)
    with open(path / EXPORT_FILE, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    return manifest


__all__ = ["ERROR_FILE", "EXPORT_FILE", "REQUEST_FILE", "ExportStore", "build_export",
           "campaign_bags", "export_dir", "export_file", "export_file_name", "iter_file",
           "plan_tables", "read_status", "rewrite_sqlite3", "write_tables"]
