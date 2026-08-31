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

"""A campaign's own record, mirrored into the central index.

``campaign.db`` stays exactly what it is: authored by the driver as the campaign runs,
migrated in place on its own ladder, and the thing that makes a campaign directory
self-describing offline. This copies it into the index so a campaign becomes *findable*
there -- listable, joinable to its metrics -- without the index becoming a second source
of truth. The direction is one-way and never reverses: ``campaign.db`` is read, the index
is written.

It is cheap to do, which is what makes the one-way rule affordable. Measured on a real
campaign, ``campaign.db`` is **80 KB across 7 tables and 46 rows** for a 20-run campaign;
the ~582k metric rows beside it are four orders of magnitude larger. Re-mirroring a
campaign from scratch costs nothing, so nothing has to be updated in place.

**The mirror is schema-driven, not a column list.** ``store.py``'s ``_SCHEMA`` has a
migration ladder (``user_version`` 9 at the time of writing) and gains columns; a
transcribed list here would be a second declaration of the same schema, drifting silently
the first time the ladder moves and dropping whatever it had not heard about. So the
columns are read from the file with ``PRAGMA table_info`` and mirrored as found. A column
added upstream arrives here for free; one removed stops being mirrored.

**Integer ids are kept verbatim and scoped, not remapped.** A per-campaign file numbers its
rows from 1, so ``unit.id = 3`` means something different in every campaign. Rather than
allocating global ids, every row carries the campaign's string id and the primary key
becomes ``(campaign_id, id)``. Two reasons: re-ingest has to be idempotent, and a remap
would hand the same row a different id on the second pass; and an id in the index still
matches the id in that campaign's own ``campaign.db``, which is what makes a support
question answerable.

The integer ``campaign_id`` that ``batch``/``job``/``node``/``container_failure`` carry is
**dropped**, because it is the only column that carries no information once the string id
is present -- a per-campaign file has exactly one campaign row, so the FK is always 1. The
FKs that do carry information (``unit.batch_id``, ``run.unit_id``, ``run.job_id``) are kept
and are read together with ``campaign_id``.
"""

import logging
import sqlite3

from robovast.results_processing import index_schema
from robovast.results_processing.csv_types import (INTEGER, REAL, TEXT, UNKNOWN)

logger = logging.getLogger(__name__)

#: The tables mirrored, parents before children so a reader can follow the FKs while a
#: mirror is in progress. Names rather than a discovery pass because *which* tables are the
#: campaign record is a decision, not a fact about the file: a future ``store.py`` table
#: holding something else should not silently become part of the index.
DIMENSION_TABLES = ("campaign", "batch", "unit", "job", "node", "run", "container_failure")

#: The integer FK that carries nothing once every row is scoped by the campaign's string
#: id -- see the module docstring.
_REDUNDANT_COLUMNS = frozenset({"campaign_id"})

#: SQLite declares these; ``csv_types`` is the vocabulary ``index_schema`` speaks.
_FROM_SQLITE = {
    "INTEGER": INTEGER, "REAL": REAL, "TEXT": TEXT, "BLOB": TEXT, "": UNKNOWN,
}

#: ``strategy_state`` is an opaque pickle the search writes and only the search reads, and
#: it is already masked out of every results surface. Mirroring it would put a blob in the
#: index that nothing can query and that no reader is allowed to see.
_EXCLUDED_COLUMNS = frozenset({"strategy_state"})


def _column_types(store: sqlite3.Connection, table: str) -> dict:
    """``{column: verdict}`` for *table*, as the file declares them."""
    types = {}
    for row in store.execute(f'PRAGMA table_info("{table}")'):
        name, decl = row[1], (row[2] or "").upper()
        if name in _REDUNDANT_COLUMNS or name in _EXCLUDED_COLUMNS:
            continue
        types[name] = _FROM_SQLITE.get(decl, TEXT)
    return types


def _tables_in(store: sqlite3.Connection) -> set:
    return {r[0] for r in store.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}


def mirror_campaign_record(conn, store_path: str, campaign_id: str) -> dict:
    """Mirror ``campaign.db`` at *store_path* into the index; return rows per table.

    Idempotent: a campaign's existing dimension rows are deleted and rewritten, so
    re-ingesting after a re-postprocess -- or after the index was dropped entirely -- lands
    the same rows rather than duplicating them. This is the cheap half of the
    reproducibility invariant, and the reason it can be a delete-and-rewrite rather than an
    upsert is the 80 KB measured above.

    A table the file does not have is skipped rather than failing: a campaign recorded
    before that table existed is still worth listing, and refusing it would make the index
    unable to hold exactly the old campaigns the corpus is made of.
    """
    written = {}
    store = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    store.row_factory = sqlite3.Row
    try:
        present = _tables_in(store)
        for table in DIMENSION_TABLES:
            if table not in present:
                logger.debug("index: %s has no %s table, skipping", store_path, table)
                continue
            types = _column_types(store, table)
            if not types:
                continue
            index_schema.ensure_table(conn, table, types, source=f"campaign.db:{table}",
                                      context=index_schema.CAMPAIGN_CONTEXT)
            conn.execute(f'DELETE FROM "{table}" WHERE campaign_id = %s', (campaign_id,))

            columns = ["campaign_id"] + list(types)
            quoted = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
            rows = store.execute(f'SELECT * FROM "{table}"')
            count = 0
            with conn.cursor().copy(f'COPY "{table}" ({quoted}) FROM STDIN') as copy:
                for row in rows:
                    copy.write_row(tuple([campaign_id] + [row[c] for c in types]))
                    count += 1
            written[table] = count
        logger.info("index: mirrored %s record for %s (%s)", store_path, campaign_id,
                    ", ".join(f"{t}={n}" for t, n in written.items()) or "empty")
        return written
    finally:
        store.close()
