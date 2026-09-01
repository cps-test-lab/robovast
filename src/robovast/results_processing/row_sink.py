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

"""Where a source's rows go: the seam between producing rows and storing them.

One interface, several sources. Today there are two -- a rosbag's handlers and the
``*.csv``/``*.jsonl`` glob over a run directory -- and neither is privileged: the glob is
not a legacy path kept alive for compatibility, it is the documented join point for any
producer RoboVAST does not own. ``docs/results_processing.rst`` states it as the pose
contract: *"A stack on some other middleware, a motion-capture ingest, or a real-robot log
joins them by emitting the same columns; nothing has to be registered."*

That sentence used to mean "write a CSV". It now means "write a CSV **or** implement this",
which is the same contract with the fast path opened up. It has to be this way round
because the *reader* can never be neutral -- ``rosbags_process.py`` is ``rosbag2_py`` plus
``rclpy`` with CDR hardcoded, and runs only inside the ROS container -- so neutrality has
to live above the reader, where it already did.

**Declared types stream; inferred types buffer, and the difference is not a preference.**
A bag knows its message schema, so a bag-derived table's column types are declared and its
rows can go straight to ``COPY`` -- which matters because a single campaign's ``sim_poses``
is ~4M rows and buffering it to look at the values would defeat the point of streaming the
bag in the first place. A CSV knows nothing: every value is a string, so the type has to be
inferred by reading them (:func:`~robovast.results_processing.csv_types.infer_column_types`),
which means buffering. That is affordable only because it is bounded -- one run's one file
-- and it is the reason the two paths look different here rather than being unified for
tidiness.

``COPY`` rather than ``INSERT`` is the whole performance story: measured against a local
Postgres 16, ``COPY`` moves ~1.7M rows/s, so a 240-run campaign's ~7.6M rows is a few
seconds. That is what replaces writing a CSV, re-reading it, inferring types per value,
inserting into SQLite, building an index, and then moving a 1.1 GB file twice.
"""

import logging

from robovast.results_processing import index_schema
from robovast.results_processing.csv_types import infer_column_types, sql_value

logger = logging.getLogger(__name__)


class RowSink:
    """Somewhere for a source to put rows.

    Deliberately minimal: a source should not have to know whether it is feeding a
    database, a file, or a test double. Everything about the destination -- the
    connection, the campaign scoping, the batching -- belongs to the implementation.
    """

    def write(self, table: str, rows, *, context: dict, types: dict = None,
              source: str = "") -> int:
        """Store *rows* in *table*; return how many were written.

        *rows* is an iterable of dicts keyed by column name. *context* is applied to
        every row of this call -- the campaign, configuration and run the batch belongs to
        -- so a source yields the columns it measured and nothing about where they sit.

        *types* maps column name to a :mod:`csv_types` verdict when the source knows them
        (a bag does). Omit it and the types are inferred, which requires buffering *rows*;
        see the module docstring on why that asymmetry is deliberate.

        *source* names this batch for a column note, so a widening says which run
        disagreed rather than only that some run did.
        """
        raise NotImplementedError


class PostgresRowSink(RowSink):
    """A :class:`RowSink` that ``COPY``s into the central index."""

    def __init__(self, conn, campaign_id: str):
        self._conn = conn
        self._campaign_id = campaign_id

    def write(self, table: str, rows, *, context: dict, types: dict = None,
              source: str = "") -> int:
        ctx = {"campaign_id": self._campaign_id, **context}

        if types is None:
            # The CSV path: nothing knows the types until the values have been read, so
            # this one batch is materialised. Bounded by one file in one run directory.
            rows = list(rows)
            data_columns = {c for row in rows for c in row}
            types = infer_column_types(rows, sorted(data_columns))

        # A context column's type is fixed by the schema, not by this batch -- a run_id of
        # 0 must not make the column narrower than the bigint every other campaign uses.
        declared = dict(index_schema.CONTEXT_COLUMNS)
        declared.update({c: t for c, t in types.items() if c not in declared})
        index_schema.ensure_table(self._conn, table, declared, source=source)

        columns = [name for name, _ in index_schema.CONTEXT_COLUMNS]
        columns += [c for c in types if c not in set(columns)]
        quoted = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
        statement = f'COPY "{table}" ({quoted}) FROM STDIN'

        written = 0
        with self._conn.cursor().copy(statement) as copy:
            for row in rows:
                merged = {**ctx, **row}
                copy.write_row(tuple(
                    sql_value(merged.get(c), types.get(c, declared.get(c)))
                    for c in columns))
                written += 1
        logger.debug("index: copied %d rows into %s%s",
                     written, table, f" from {source}" if source else "")
        return written
