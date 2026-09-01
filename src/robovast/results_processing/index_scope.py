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

"""Making the campaign a property of the *session*, not of the caller's memory.

One index holds every campaign, so ``FROM run_view`` with no predicate is a query over
the whole corpus. That is not a hypothetical: the results tree in the web UI shipped
exactly that query and rendered another campaign's runs inside the campaign the user had
opened. **A forgotten predicate is silent** -- it returns more rows that look entirely
ordinary -- so it cannot be left to every notebook, panel and third-party plugin to
remember.

The scope is therefore enforced by Postgres itself, through row-level security keyed on a
session setting:

* a scoped session sets ``robovast.campaign_id`` to the campaign (or the comma-separated
  list, for a deliberate cross-campaign query);
* every relation carrying ``campaign_id`` has a policy admitting a row only when it
  matches that setting;
* when the setting is absent or empty the policy admits everything, which is what leaves
  the ingest -- and a deliberately corpus-wide maintenance query -- untouched.

Rewriting the caller's SQL was the alternative and is rejected: injecting a predicate
means parsing SQL, and a parser that is *nearly* right silently scopes the wrong subquery.

**Three Postgres facts this depends on, each verified against the deployment's Postgres 16
rather than assumed** (a wrong assumption here does not fail, it leaks):

1. **A superuser bypasses RLS entirely**, and ``FORCE ROW LEVEL SECURITY`` does not change
   that -- it only removes the *owner's* exemption. The index's role is routinely the
   database owner and frequently a superuser, so binding the scope to the connecting role
   would enforce nothing. A scoped session therefore ``SET ROLE``s to
   :data:`READER_ROLE`, an unprivileged ``NOLOGIN`` role that holds ``SELECT`` and nothing
   else, and RLS applies to it in full.
2. **A view does not inherit its tables' RLS**; it runs with its owner's rights, which is
   precisely how the leak above reached the browser through ``run_view``. Views are
   created ``WITH (security_invoker = true)`` (Postgres 15+) so the reader's policies
   apply to what the view reads.
3. **``COPY ... FROM`` works on a table with RLS forced**, provided the policy's
   ``WITH CHECK`` admits the row -- so the ingest, which writes by ``COPY`` and sets no
   scope, keeps writing every campaign.

This is a guard against *forgetting*, not a security boundary: a session that reaches the
index can ``RESET ROLE`` as easily as it can drop a ``WHERE`` clause. It sits at exactly
the same level as the read-only session the query layer already relies on.
"""

import logging

from robovast.results_processing import index_schema

logger = logging.getLogger(__name__)

#: The session setting the policies read. A custom GUC, so it needs the dotted prefix.
SCOPE_SETTING = "robovast.campaign_id"

#: The unprivileged role a scoped read runs as -- see fact (1) in the module docstring.
#: ``NOLOGIN``: nothing connects as it, sessions arrive as the index's own role and
#: ``SET ROLE`` into it.
READER_ROLE = "robovast_reader"

#: One policy name on every scoped relation, so the sweep can tell "already secured" from
#: "never secured" without inspecting the expression.
POLICY_NAME = "robovast_campaign_scope"

#: The column that carries the scope. A relation without it is not campaign-scoped data.
SCOPE_COLUMN = "campaign_id"

#: Admits a row when the session names no scope, or when the row's campaign is one of the
#: named ones. The empty-scope arm is what leaves the ingest and deliberate corpus-wide
#: work unaffected; ``WITH CHECK (true)`` is what leaves writes to the owner alone, since
#: the ingest must be able to write every campaign.
_POLICY_USING = (
    f"coalesce(current_setting('{SCOPE_SETTING}', true), '') = '' "
    f"OR {SCOPE_COLUMN} = ANY("
    f"string_to_array(current_setting('{SCOPE_SETTING}', true), ','))")


class ScopeNotEnforceable(RuntimeError):
    """The session could not be confined to the campaign it was opened for.

    Raised rather than falling back to an unscoped session: an unscoped read of one shared
    index answers with the corpus, in the shape and the columns the caller expected. That
    is the failure this module exists to prevent, so it must never be the fallback for
    this module failing.
    """

    include_traceback = False


def _scoped_relations(conn) -> list:
    """``[(kind, schema, name, secured)]`` for everything the scope must cover.

    ``kind`` is ``'table'`` or ``'view'``; *secured* says whether it already carries what
    its kind needs -- RLS enabled, forced and policied for a table, ``security_invoker``
    for a view. Read from ``pg_catalog`` in one statement because it runs on every scoped
    read: ``information_schema`` answers the same question over several joins of views.
    """
    schemas = [conn.execute("SELECT current_schema()").fetchone()[0] or "public",
               index_schema.CAMPAIGN_SCHEMA]
    rows = conn.execute(
        """
        SELECT CASE c.relkind WHEN 'v' THEN 'view' ELSE 'table' END,
               n.nspname, c.relname,
               CASE WHEN c.relkind = 'v'
                    THEN coalesce('security_invoker=true' = ANY(c.reloptions)
                                  OR 'security_invoker=on' = ANY(c.reloptions), false)
                    ELSE c.relrowsecurity AND c.relforcerowsecurity
                         AND EXISTS (SELECT 1 FROM pg_policy p
                                     WHERE p.polrelid = c.oid AND p.polname = %s)
               END
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = %s AND NOT a.attisdropped
        WHERE c.relkind IN ('r', 'v') AND n.nspname = ANY(%s)
        ORDER BY n.nspname, c.relname
        """, (POLICY_NAME, SCOPE_COLUMN, schemas)).fetchall()
    return [(kind, schema, name, bool(secured)) for kind, schema, name, secured in rows]


def ensure_reader_role(conn) -> None:
    """Create :data:`READER_ROLE` if it is absent and make it reachable by ``SET ROLE``.

    Idempotent and race-tolerant: two ingests, or two test suites against a shared
    cluster, may reach this at the same time and a role is cluster-wide.

    The membership grant is not optional. ``SET ROLE`` needs the connecting role to be a
    member (superusers excepted), and a deployment whose index role is a plain owner would
    otherwise fail to scope anything -- loudly, but for a reason that reads as unrelated.
    """
    from psycopg import errors  # pylint: disable=import-outside-toplevel

    try:
        with conn.transaction():
            conn.execute(f'CREATE ROLE "{READER_ROLE}" NOLOGIN')
    except errors.DuplicateObject:
        pass
    except errors.InsufficientPrivilege as exc:
        # Said plainly, because the raw message ("permission denied to create role") does
        # not connect to what the reader will see: unscoped answers, or none.
        raise ScopeNotEnforceable(
            f"The index role may not create the {READER_ROLE} role, which is how a query "
            "is confined to one campaign (the owner and any superuser bypass row-level "
            "security). Grant CREATEROLE to the index role once, or create "
            f'"{READER_ROLE}" NOLOGIN and grant it to the index role by hand.') from exc
    with conn.transaction():
        conn.execute(f'GRANT "{READER_ROLE}" TO CURRENT_USER')


def secure_table(conn, table: str, schema: str = index_schema.METRIC_SCHEMA) -> None:
    """Put the campaign policy on one table and let the reader role select from it.

    Called from :func:`~robovast.results_processing.index_schema.ensure_table`, because
    tables appear as data files appear -- a ``poses.csv`` in a run directory creates
    ``poses`` -- so securing the index once at setup would leave every table created after
    it unscoped, which is to say leaking.

    A table without a ``campaign_id`` column is left alone: the bookkeeping tables
    (``_column_types``, ``_column_notes``, ``_table_name_map``) describe the *index's*
    schema rather than any campaign's rows, and there is no key to scope them by. RLS
    enabled with no policy denies everything, so enabling it there would only break
    ``describe``. ``_campaigns`` does carry ``campaign_id`` and is scoped like the rest --
    a scoped session has no business enumerating the corpus.
    """
    ensure_reader_role(conn)
    name = index_schema.qualified(table, schema)
    # The metric schema is the empty string -- "wherever search_path points" -- and it
    # still needs the USAGE grant: without it the schema drops out of the reader's
    # effective search path and every table in it reports "relation does not exist",
    # which reads as a missing ingest rather than a missing grant.
    live = schema or conn.execute("SELECT current_schema()").fetchone()[0] or "public"
    conn.execute(f'GRANT USAGE ON SCHEMA {index_schema.qualified(live)} '
                 f'TO "{READER_ROLE}"')
    conn.execute(f'GRANT SELECT ON {name} TO "{READER_ROLE}"')

    has_scope = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = coalesce(nullif(%s, ''), current_schema()) "
        "AND table_name = %s AND column_name = %s",
        (schema, table, SCOPE_COLUMN)).fetchone()
    if not has_scope:
        return

    conn.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    # FORCE removes the owner's exemption. The ingest connects as the owner and sets no
    # scope, so it still writes and deletes every campaign; what this closes is an
    # owner-role *reader* that never reaches SET ROLE.
    conn.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
    exists = conn.execute(
        "SELECT 1 FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE p.polname = %s AND c.relname = %s "
        "AND n.nspname = coalesce(nullif(%s, ''), current_schema())",
        (POLICY_NAME, table, schema)).fetchone()
    if not exists:
        conn.execute(f'CREATE POLICY "{POLICY_NAME}" ON {name} '
                     f"USING ({_POLICY_USING}) WITH CHECK (true)")


def secure_view(conn, view: str) -> None:
    """Let the reader role select from a view. Its RLS comes from ``security_invoker``.

    The flag is set at ``CREATE VIEW`` time (see
    :mod:`~robovast.results_processing.index_views`) rather than here, because a view
    created without it and patched afterwards is unscoped for exactly as long as the two
    statements are apart -- and because that window is where the original leak lived.
    """
    ensure_reader_role(conn)
    conn.execute(f'GRANT SELECT ON "{view}" TO "{READER_ROLE}"')


def apply_to_index(conn) -> list:
    """Secure every relation the index already holds; return what had to be repaired.

    The backfill for an index that predates this module, and the safety net for anything
    created outside :func:`secure_table` -- a hand-made table, a view added by a
    deployment. Cheap: it is catalog work over tens of relations, and the ingest runs it
    once.
    """
    ensure_reader_role(conn)
    # Blanket rather than per relation: the reader needs SELECT on the unkeyed bookkeeping
    # tables too (``describe`` reads the column notes), and "everything in these two
    # schemas" is both the honest description of what a reader may see and one statement
    # instead of a loop that has to stay in step with the catalog.
    for schema in (conn.execute("SELECT current_schema()").fetchone()[0] or "public",
                   index_schema.CAMPAIGN_SCHEMA):
        # The campaign schema exists only once a campaign record has been mirrored; an
        # index holding measurements alone is a normal state, not a fault.
        if not conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s",
                            (schema,)).fetchone():
            continue
        conn.execute(f'GRANT USAGE ON SCHEMA {index_schema.qualified(schema)} '
                     f'TO "{READER_ROLE}"')
        conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA '
                     f'{index_schema.qualified(schema)} TO "{READER_ROLE}"')

    repaired = []
    for kind, schema, name, secured in _scoped_relations(conn):
        if secured:
            continue
        if kind == "view":
            conn.execute(f'ALTER VIEW {index_schema.qualified(name, schema)} '
                         "SET (security_invoker = true)")
            secure_view(conn, name)
        else:
            secure_table(conn, name, schema)
        repaired.append(f"{schema}.{name}")
    if repaired:
        logger.info("index: campaign scope applied to %s", ", ".join(repaired))
    return repaired


def assert_enforceable(conn) -> None:
    """Raise unless every campaign-scoped relation would actually be filtered.

    Run before a scoped session is handed to a caller, and the reason this module can
    promise anything: a table created before the policy existed, or a view created without
    ``security_invoker``, does not error when queried -- it answers with the corpus. The
    check is one catalog statement, and naming the relation is what makes the repair
    obvious.
    """
    unsecured = [f"{schema}.{name} ({kind})"
                 for kind, schema, name, secured in _scoped_relations(conn) if not secured]
    if unsecured:
        raise ScopeNotEnforceable(
            "The campaign scope cannot be enforced: " + ", ".join(sorted(unsecured))
            + " carries campaign_id but is not covered by row-level security, so a query "
              "against it would return every campaign's rows. Re-run postprocessing for "
              "any campaign (the ingest repairs the index) or call "
              "index_scope.apply_to_index() on a writable connection.")


def enter_scope(conn, campaign_ids) -> None:
    """Confine this session to *campaign_ids* for the rest of its life.

    Not ``SET LOCAL``: the query layer's connections are autocommit, where ``SET LOCAL``
    applies to a transaction that ends with the statement that opened it -- which would
    leave the very next statement unscoped. The connection is opened per query and closed
    after it, so a session setting has the intended lifetime.
    """
    ids = [str(cid).strip() for cid in campaign_ids if str(cid).strip()]
    if not ids:
        raise ScopeNotEnforceable(
            "A campaign scope was requested with no campaign id. An empty scope is how "
            "the ingest asks for the whole corpus, so accepting it here would silently "
            "widen a scoped read to every campaign.")
    # The GUC is one string, so a comma in an id would split it into two scopes -- and the
    # halves would match nothing, which reads as "this campaign is empty".
    bad = [cid for cid in ids if "," in cid]
    if bad:
        raise ScopeNotEnforceable(
            f"campaign id may not contain a comma: {', '.join(bad)}")

    ensure_reader_role(conn)
    assert_enforceable(conn)
    # Role first: the setting is read by policies that only bind once the session is no
    # longer the owner (nor a superuser, which bypasses RLS whatever the policies say).
    conn.execute(f'SET ROLE "{READER_ROLE}"')
    # set_config, not SET: ``SET`` takes no parameters, and a campaign id is a directory
    # name from disk -- the one value here that must never be spliced into a statement.
    # ``false`` makes it a session setting rather than a transaction-local one; see the
    # docstring on why the transaction-local form would expire immediately.
    conn.execute(f"SELECT set_config('{SCOPE_SETTING}', %s, false)", (",".join(ids),))
    active = conn.execute(f"SELECT current_setting('{SCOPE_SETTING}', true), "
                          "current_user").fetchone()
    if active[0] != ",".join(ids) or active[1] != READER_ROLE:
        raise ScopeNotEnforceable(
            f"the session did not take the campaign scope (role {active[1]}, "
            f"scope {active[0]!r})")
