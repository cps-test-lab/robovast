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

"""A finished campaign's tables, one file each.

A table is built one file per run, which is what lets a query build only the runs it names
and a live run append as it records. Once a campaign's runs are final that layout only costs:
thousands of small files, each carrying its own footer and its own copy of the columns that
name the run, and a query that opens every one of them. **Compacting** merges a table's final run
files into one file, and its run entries stay in the manifest, marked ``compacted``, still saying
what each run's rows were built from.

**Nothing is lost and nothing is re-decoded.** The file holds exactly the rows the run files
held, in their order, run after run; the columns are the union of the runs' columns, context
columns first and the rest in the order they first appear, with a column that is all ``null``
in one run taking the type it has in the others. A table whose runs disagree on a column's
type is left as it is and reported.

**Sorted by run, so a narrow read stays narrow.** Rows are written in ``config_name``,
``run_id`` order, so every row group's statistics name few runs and a query for one
configuration or one run skips the rest of the file.

**Each column is stored the way it is smallest.** A floating-point column is written with the
dictionary, plain or byte-stream-split encoding, whichever a trial write of a sample of it
makes smallest: byte-stream-split wins for most sensor values and loses for values that
repeat.

**A compacted run can still be built again.** Its entry is then written anew, with its own file,
and a reader takes that run's rows from there; the next compaction folds it back in. Only entries a
reader may take as they are -- written by this decoder under this contract, final, with no
reason -- are compacted, so a run still recording keeps its own files.

**A reader never sees a row twice.** Every compaction writes a file of a new name, the manifest then
names it in place of the run files it replaces, and those files are removed only after the
manifest is on disk; a query that read the manifest before reads the run files it named.
"""

from __future__ import annotations

import io
import multiprocessing
import os
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Iterator, List, Optional

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .tables import (CONTEXT_COLUMNS, TABLES_DIR, _incoming, cache_root, compacted_runs,
                     manifest_lock, read_manifest, record_campaign_table, remove_files,
                     write_manifest, written_here)

#: zstd level of a compacted file. A compaction is written once and read many times; past this level
#: the files shrink by little and the writing takes several times as long.
ZSTD_LEVEL = 9

#: Rows per row group: large enough to compress well, small enough that one run's rows span
#: few groups and a query for it skips the rest.
ROW_GROUP_ROWS = 122_880

#: Rows a column's encoding is chosen on.
SAMPLE_ROWS = 200_000

_ENCODINGS = {
    "dictionary": {},
    "plain": {"use_dictionary": False},
    "byte_stream_split": {"use_dictionary": False, "column_encoding": "BYTE_STREAM_SPLIT"},
}


@dataclass
class CompactReport:
    """What a compaction did, per table."""
    #: ``{table: runs compacted into its file}``.
    compacted: Dict[str, int] = field(default_factory=dict)
    #: ``{table: why it was left as it is}``.
    skipped: Dict[str, str] = field(default_factory=dict)
    #: Bytes of the files replaced, and of the files that replaced them.
    bytes_before: int = 0
    bytes_after: int = 0


@dataclass
class _Plan:
    table: str
    #: The campaign file an earlier compaction wrote, and the runs of it that stay.
    previous: List[str]
    carried: List[str]
    #: ``{run key: its files}`` of the runs compacted now.
    runs: Dict[str, List[str]]


@dataclass
class _Written:
    table: str
    rel: str
    rows: int
    schema: pa.Schema
    keys: List[str]
    replaced: List[str]
    bytes_before: int
    error: str = ""


def compacted_path(table: str) -> str:
    """A new file name for *table*'s compacted rows, relative to the cache root."""
    return os.path.join(TABLES_DIR, table, f"_compacted-{uuid.uuid4().hex[:12]}.parquet")


def _plans(manifest: dict, tables: Optional[Iterable[str]]) -> List[_Plan]:
    wanted = set(tables) if tables is not None else None
    plans = []
    for table, entry in sorted(manifest.get("tables", {}).items()):
        if wanted is not None and table not in wanted:
            continue
        runs = {}
        for key, run in sorted(entry.get("runs", {}).items()):
            if (written_here(run) and run.get("complete") and run.get("live") is None
                    and not run.get("reason") and run.get("files")):
                runs[key] = list(run["files"])
        if not runs:
            continue
        carried = sorted(compacted_runs(manifest, table) - set(runs))
        whole = entry.get("campaign") or {}
        previous = list(whole.get("files") or []) if whole.get("compacted") is not None else []
        plans.append(_Plan(table, previous, carried if previous else [], runs))
    return plans


def _key_column(table: pa.Table) -> pa.Array:
    # pyarrow.compute's functions are generated at import, which pylint cannot see.
    return pc.binary_join_element_wise(  # pylint: disable=no-member
        table["config_name"], pc.cast(table["run_id"], pa.string()), "/")


def _sources(root: str, plan: _Plan) -> Iterator[pa.Table]:
    """The rows to compact, run after run: those an earlier compaction keeps, then the runs' files."""
    carried = set(plan.carried)
    for rel in plan.previous:
        parquet = pq.ParquetFile(os.path.join(root, rel))
        for batch in parquet.iter_batches(batch_size=ROW_GROUP_ROWS):
            rows = pa.Table.from_batches([batch])
            keep = pc.is_in(_key_column(rows),  # pylint: disable=no-member
                            value_set=pa.array(sorted(carried)))
            rows = rows.filter(keep)
            if rows.num_rows:
                yield rows
    for key in sorted(plan.runs):
        for rel in plan.runs[key]:
            yield pq.read_table(os.path.join(root, rel))


def _schema(root: str, plan: _Plan) -> pa.Schema:
    """The union of the sources' columns, context first, types unified."""
    schemas = [pq.read_schema(os.path.join(root, rel)) for rel in plan.previous]
    schemas += [pq.read_schema(os.path.join(root, rel))
                for key in sorted(plan.runs) for rel in plan.runs[key]]
    unified = pa.unify_schemas(schemas, promote_options="permissive")
    context = [unified.field(c) for c in CONTEXT_COLUMNS if c in unified.names]
    rest = [f for f in unified if f.name not in CONTEXT_COLUMNS]
    return pa.schema(context + rest)


def _conform(rows: pa.Table, schema: pa.Schema) -> pa.Table:
    columns = [rows.column(f.name) if f.name in rows.column_names
               else pa.nulls(rows.num_rows, f.type) for f in schema]
    return pa.Table.from_arrays(columns, names=schema.names).cast(schema)


def _is_float(dtype: pa.DataType) -> bool:
    if pa.types.is_list(dtype) or pa.types.is_large_list(dtype):
        return pa.types.is_floating(dtype.value_type)
    return pa.types.is_floating(dtype)


def _leaf(f: pa.Field) -> str:
    """The column path an encoding is set on: a list's values, not the list."""
    if pa.types.is_list(f.type) or pa.types.is_large_list(f.type):
        return f"{f.name}.list.element"
    return f.name


def _smallest(column: pa.ChunkedArray, f: pa.Field, level: int) -> str:
    best, size = "dictionary", None
    for name, options in _ENCODINGS.items():
        options = dict(options)
        if "column_encoding" in options:
            options["column_encoding"] = {_leaf(f): options["column_encoding"]}
        buf = io.BytesIO()
        pq.write_table(pa.table({f.name: column}), buf, compression="zstd",
                       compression_level=level, **options)
        if size is None or buf.tell() < size:
            best, size = name, buf.tell()
    return best


def _writer_options(sample: pa.Table, schema: pa.Schema, level: int) -> dict:
    chosen = {f.name: _smallest(sample.column(f.name), f, level)
              for f in schema if _is_float(f.type)}
    plain = {name for name, how in chosen.items() if how != "dictionary"}
    split = {_leaf(schema.field(name)): "BYTE_STREAM_SPLIT"
             for name, how in chosen.items() if how == "byte_stream_split"}
    # Both options name a column by its leaf path: a list column's encoding is its values'.
    return {"use_dictionary": [_leaf(f) for f in schema if f.name not in plain],
            "column_encoding": split or None}


def _compact_table(campaign_dir: str, plan: _Plan, level: int) -> _Written:
    """Write *plan*'s rows to a new file; the manifest is not touched."""
    root = cache_root(campaign_dir)
    rel = compacted_path(plan.table)
    path = os.path.join(root, rel)
    replaced = list(plan.previous) + [r for key in sorted(plan.runs) for r in plan.runs[key]]
    before = sum(os.path.getsize(os.path.join(root, r)) for r in replaced)
    keys = sorted(set(plan.carried) | set(plan.runs))
    try:
        schema = _schema(root, plan)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        return _Written(plan.table, rel, 0, None, keys, [], before,
                        error=f"its runs' columns do not agree: {exc}")
    tmp = _incoming(path)
    writer = None
    pending: List[pa.Table] = []
    held = 0
    rows = 0

    def emit(final: bool) -> None:
        nonlocal pending, held, writer
        if not pending:
            return
        buffered = pa.concat_tables(pending)
        if writer is None:
            options = _writer_options(buffered.slice(0, SAMPLE_ROWS), schema, level)
            writer = pq.ParquetWriter(tmp, schema, compression="zstd",
                                      compression_level=level, write_statistics=True,
                                      **options)
        cut = buffered.num_rows if final else (buffered.num_rows // ROW_GROUP_ROWS
                                               * ROW_GROUP_ROWS)
        if cut:
            writer.write_table(buffered.slice(0, cut), row_group_size=ROW_GROUP_ROWS)
        rest = buffered.slice(cut)
        pending = [rest] if rest.num_rows else []
        held = rest.num_rows

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for part in _sources(root, plan):
            part = _conform(part, schema)
            pending.append(part)
            held += part.num_rows
            rows += part.num_rows
            # The first group waits for the sample the encodings are chosen on.
            if held >= max(ROW_GROUP_ROWS, SAMPLE_ROWS if writer is None else 0):
                emit(final=False)
        emit(final=True)
        if writer is None:
            writer = pq.ParquetWriter(tmp, schema, compression="zstd", compression_level=level)
        writer.close()
        writer = None
        os.replace(tmp, path)
    finally:
        if writer is not None:
            writer.close()
        if os.path.exists(tmp):
            os.unlink(tmp)
    return _Written(plan.table, rel, rows, schema, keys, replaced, before)


def _still_as_planned(manifest: dict, plan: _Plan) -> bool:
    """Whether nothing *plan* was made from changed while its file was written."""
    entry = manifest.get("tables", {}).get(plan.table, {})
    runs = entry.get("runs", {})
    if any((runs.get(key) or {}).get("files") != files for key, files in plan.runs.items()):
        return False
    previous = (entry.get("campaign") or {}).get("files") or []
    if plan.previous and previous != plan.previous:
        return False
    return set(plan.carried) <= compacted_runs(manifest, plan.table)


def compact(campaign_dir: str, tables: Optional[Iterable[str]] = None, *,
         workers: int = 1, level: int = ZSTD_LEVEL,
         progress: Optional[Callable[[int, int], None]] = None) -> CompactReport:
    """Merge each table's final run files into one file (every table, for ``None``).

    *workers* writes that many tables at once in separate processes; *progress* is called
    with ``(done, total)`` in tables.
    """
    campaign_dir = os.path.abspath(campaign_dir)
    plans = _plans(read_manifest(campaign_dir), tables)
    report = CompactReport()
    total = len(plans)
    if workers <= 1 or total <= 1:
        for done, plan in enumerate(plans, 1):
            _commit(campaign_dir, plan, _compact_table(campaign_dir, plan, level), report)
            if progress:
                progress(done, total)
        return report
    with ProcessPoolExecutor(max_workers=min(workers, total),
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {pool.submit(_compact_table, campaign_dir, plan, level): plan
                   for plan in plans}
        for done, future in enumerate(as_completed(futures), 1):
            _commit(campaign_dir, futures[future], future.result(), report)
            if progress:
                progress(done, total)
    return report


def _commit(campaign_dir: str, plan: _Plan, out: _Written, report: CompactReport) -> None:
    """Name *out* in the manifest in place of the files it replaces, then remove those.

    One table at a time, as each is written: the run files it replaces go as soon as it is
    in, so a compaction needs room for the run files and one compacted table, not a second copy of
    them all.
    """
    if out.error:
        report.skipped[plan.table] = out.error
        return
    root = cache_root(campaign_dir)
    with manifest_lock(campaign_dir):
        manifest = read_manifest(campaign_dir)
        if not _still_as_planned(manifest, plan):
            discard = [out.rel]
            report.skipped[plan.table] = "its entries changed while it was compacted"
        else:
            record_campaign_table(manifest, plan.table, files=[out.rel], rows=out.rows,
                                  schema=out.schema, sources={}, runs=len(out.keys),
                                  compacted=out.keys)
            runs = manifest["tables"][plan.table]["runs"]
            for key in plan.runs:
                runs[key]["files"] = []
                runs[key]["compacted"] = True
            write_manifest(campaign_dir, manifest)
            discard = out.replaced
            report.compacted[plan.table] = len(out.keys)
            report.bytes_before += out.bytes_before
            report.bytes_after += os.path.getsize(os.path.join(root, out.rel))
    remove_files(campaign_dir, discard)
    for directory in sorted({os.path.dirname(os.path.join(root, rel)) for rel in discard}):
        if directory != os.path.join(root, TABLES_DIR, plan.table):
            try:
                os.rmdir(directory)             # a configuration's directory, once emptied
            except OSError:
                pass                            # not empty: a run still has its own file


__all__ = ["ROW_GROUP_ROWS", "SAMPLE_ROWS", "CompactReport", "ZSTD_LEVEL", "compact", "compacted_path"]
