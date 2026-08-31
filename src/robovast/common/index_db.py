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

"""Connecting to the central index, and failing clearly when it is not there.

One index holds every campaign, so a campaign is a ``WHERE`` clause rather than an
attached database. That is what makes comparing the nine campaigns of a search arm one
query instead of ~10 GB of materialised per-campaign files, and it is why the connection
is a service-level fact rather than something derived from a campaign directory.

It lives in ``common`` because both directions need it and neither may depend on the
other: postprocessing writes rows, the service and the notebooks read them.

**There is no degraded mode, deliberately.** Postgres is a hard dependency of both lanes.
A reader that answered "no rows" when the index is unreachable would render an empty
campaign as a finished one, which is worse than an error and indistinguishable from a real
result. So a missing DSN and an unreachable server are both raised, by name, with the
endpoint to probe -- see :class:`~robovast.common.errors.IndexUnreachableError`.

The driver is only imported inside the functions that use it. ``common`` is on the import
path of the container-side scripts and of the client-facing CLI, and neither should fail
to start because a database driver is absent from an image that never talks to one.
"""

import logging
import os
import re
from typing import TYPE_CHECKING

from robovast.common.errors import IndexUnreachableError

if TYPE_CHECKING:  # pragma: no cover - for type checkers and linters only
    # The driver is imported for its type alone. At runtime it is imported inside the
    # functions that need it (see the module docstring), so an image without it still
    # imports this module; a static analyser that cannot see the return type otherwise
    # reports every `conn.execute` in every caller as an error on a missing member.
    import psycopg

logger = logging.getLogger(__name__)

#: Where the index is. A libpq connection string or URI, e.g.
#: ``host=localhost port=5432 dbname=robovast user=robovast password=...``.
DSN_ENV = "ROBOVAST_INDEX_DSN"

#: Anything that looks like a secret in a DSN, for the endpoint description below. A DSN
#: is the one config value that carries a credential, and it reaches an error message that
#: reaches a log and a browser -- so the stripping is part of the contract, not hygiene.
_SECRET_RE = re.compile(r"(password|passfile)\s*=\s*\S+", re.IGNORECASE)
_URI_CREDENTIALS_RE = re.compile(r"(?<=://)[^/@\s]+:[^/@\s]+(?=@)")


def describe_endpoint(dsn: str) -> str:
    """*dsn* with any credential removed, for an error message or a log line.

    Never assume a DSN is safe to print. It is the only piece of RoboVAST
    configuration that routinely carries a password, and an unreachable-index error is
    exactly the message most likely to be pasted into an issue.
    """
    cleaned = _SECRET_RE.sub(r"\1=***", dsn)
    return _URI_CREDENTIALS_RE.sub("***", cleaned).strip()


def index_dsn(dsn: str = None) -> str:
    """The configured DSN, or raise naming the variable that is missing."""
    resolved = dsn or os.environ.get(DSN_ENV, "").strip()
    if not resolved:
        raise IndexUnreachableError(
            f"The central index is not configured: set {DSN_ENV} to a Postgres "
            "connection string (for example "
            "'host=localhost port=5432 dbname=robovast user=robovast password=...'). "
            "Every campaign's data lives there, so nothing can be read or written "
            "without it.")
    return resolved


def connect(dsn: str = None, *, readonly: bool = False,
            autocommit: bool = True) -> "psycopg.Connection":
    """Connect to the index, translating a failure to reach it into one sentence.

    *readonly* opens the session with ``default_transaction_read_only``, which is the
    belt to the query layer's braces: the role a reader uses is ``SELECT``-only, and this
    makes a mistaken write fail on the connection rather than depending on the grant
    being right. It is not a substitute for the role.

    *autocommit* is the default because the ingest is a sequence of independent
    ``COPY``s and DDL statements: wrapping a whole campaign in one transaction would mean
    a single bad batch discarding every row before it, where the design calls for the
    batch to be reported and the rest to stay queryable.
    """
    resolved = index_dsn(dsn)
    try:
        import psycopg  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        raise IndexUnreachableError(
            "The central index needs the psycopg driver, which is not installed in this "
            "environment. It is a dependency of the robovast package; an image or venv "
            "without it cannot read or write campaign data.") from exc

    try:
        conn = psycopg.connect(resolved, autocommit=autocommit)
    except psycopg.OperationalError as exc:
        # OperationalError is psycopg's "could not talk to the server at all": refused,
        # timed out, authentication rejected, database absent. A programming error in
        # caller SQL is a different class and must not be caught here.
        raise IndexUnreachableError(
            f"The central index at {describe_endpoint(resolved)} did not answer: "
            f"{str(exc).strip() or exc.__class__.__name__}") from exc

    if readonly:
        # pylint infers the local psycopg import rather than the annotation above; the
        # Connection does have execute().
        conn.execute(  # pylint: disable=no-member
            "SET default_transaction_read_only = on")
    return conn
