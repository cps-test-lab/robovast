# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Reaching the central index, and what is said when it cannot be reached.

Most of these need no database: the contract under test is that a missing or unreachable
index is reported by name rather than degraded into an empty answer, which is the failure
mode the whole design exists to avoid. The one test that does connect skips without
``ROBOVAST_TEST_PG_DSN``.
"""

import os

import pytest

from robovast.common import index_db
from robovast.common.errors import IndexUnreachableError


def test_a_missing_dsn_names_the_variable_to_set(monkeypatch):
    """Not "no data" -- a sentence saying what is unset and why it matters."""
    monkeypatch.delenv(index_db.DSN_ENV, raising=False)

    with pytest.raises(IndexUnreachableError) as excinfo:
        index_db.index_dsn()

    message = str(excinfo.value)
    assert index_db.DSN_ENV in message
    assert "host=" in message, "the message should show the shape of what is wanted"


def test_a_blank_dsn_counts_as_missing(monkeypatch):
    """An env var set to whitespace is a deployment mistake, not a configuration."""
    monkeypatch.setenv(index_db.DSN_ENV, "   ")

    with pytest.raises(IndexUnreachableError):
        index_db.index_dsn()


def test_an_explicit_dsn_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv(index_db.DSN_ENV, "host=from-env")
    assert index_db.index_dsn("host=explicit") == "host=explicit"


def test_the_password_is_stripped_from_a_keyword_dsn():
    """An unreachable-index error is the message most likely to be pasted into an issue."""
    described = index_db.describe_endpoint(
        "host=db.internal port=5432 dbname=robovast user=rv password=s3cr3t")

    assert "s3cr3t" not in described
    assert "password=***" in described
    assert "dbname=robovast" in described, "the part worth probing must survive"


def test_the_password_is_stripped_from_a_uri_dsn():
    """The other spelling libpq accepts, which the keyword pattern does not catch."""
    described = index_db.describe_endpoint(
        "postgresql://rv:s3cr3t@db.internal:5432/robovast")

    assert "s3cr3t" not in described
    assert "db.internal:5432/robovast" in described


def test_an_unreachable_server_is_named_not_swallowed(monkeypatch):
    """The load-bearing case: down must not look like empty.

    A reader that returned no rows here would render an unfinished campaign as a
    finished one, which is indistinguishable from a real result.
    """
    pytest.importorskip("psycopg")
    # Port 1 is reserved and never listening, so this exercises the real driver path
    # rather than a stubbed exception.
    monkeypatch.setenv(index_db.DSN_ENV,
                       "host=127.0.0.1 port=1 dbname=robovast connect_timeout=2")

    with pytest.raises(IndexUnreachableError) as excinfo:
        index_db.connect()

    message = str(excinfo.value)
    assert "127.0.0.1" in message and "did not answer" in message
    assert excinfo.value.include_traceback is False


def test_a_readonly_session_refuses_a_write():
    """Belt to the role's braces: a mistaken write fails on the connection."""
    dsn = os.environ.get("ROBOVAST_TEST_PG_DSN")
    if not dsn:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
    psycopg = pytest.importorskip("psycopg")

    with index_db.connect(dsn, readonly=True) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TABLE should_not_exist (a int)")
