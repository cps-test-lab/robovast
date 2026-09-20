# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Provision the Postgres the index tests run against.

The index tests are gated on ``ROBOVAST_TEST_PG_DSN``, and with no database ~240 of
them skip -- a suite reporting green while the correctness coverage of everything that
reads or writes campaign results has not run. A suite that quietly skips its most
important tests is worse than one that fails, so the database is the suite's own
responsibility rather than an operator's.

Mechanism: the ``docker`` CLI plus the ``psycopg`` that is already a test dependency.
``pytest-postgresql`` and ``testcontainers`` would each add a dependency (and
``pytest-postgresql`` additionally needs a local ``initdb``/``pg_ctl``, which this
environment does not have) to do the same thing; the CLI needs nothing new, and
``robovast_client`` keeps its empty dependency set.

Timing: this runs from ``pytest_configure``, *not* a session fixture, because the
gates are module-level -- ``DSN = os.environ.get(...)`` and ``pytest.mark.skipif`` are
evaluated at import time, which is collection, which is after every fixture would be
too late. ``pytest_configure`` is the last hook that still precedes collection.

**One server, a database per session.** The container is named and outlives the run
that started it, because starting one costs a couple of seconds and a developer runs
pytest far more often than that is worth -- only the first run on a machine pays it.
What each session gets instead is its own ``CREATE DATABASE`` inside that server, which
costs a fraction of a second and is what keeps two suites running at once from meeting:
they share a server but never a table, a schema or a search_path. The database is
dropped when the session ends, and one left by a session that was killed is dropped by
the next run that sees its owner is gone.

The server is a test fixture, not a service: it publishes a random host port (``-P``)
so it cannot collide with a Postgres already running here, its data directory is a
tmpfs, and it runs with ``fsync=off``. Remove it with ``docker rm -f`` and the next run
starts another.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import NamedTuple

#: The env var the tests read. Set by the caller, honoured when already set.
DSN_ENV = "ROBOVAST_TEST_PG_DSN"

#: Small, cached, and Postgres 16 like the deployment. Pinned: a test database that
#: drifts with :latest turns an upstream release into a mystery failure here.
IMAGE = "postgres:16-alpine"

#: The server's container name. Fixed rather than generated: it is how a later run
#: finds the server this one started, and how a developer removes it by hand.
CONTAINER_NAME = "robovast-test-pg"

#: Marks the container as ours, for `docker ps --filter label=`.
OWNER_LABEL = "robovast-test-pg"

#: A session's database is named for the process that owns it, so a later run can tell
#: a database still in use from one whose session died before it could drop it.
DATABASE_PREFIX = "rvtest_"

#: The database the server creates for itself, which sessions connect to in order to
#: create and drop their own. Never used by a test.
ADMIN_DATABASE = "robovast_test"

_PASSWORD = "robovast-test"

_STARTUP_TIMEOUT_S = 60.0


class ProvisionError(RuntimeError):
    """Raised with a message naming what is missing and how to get it."""


def _run(*args: str, timeout: float = 120.0) -> str:
    result = subprocess.run(args, capture_output=True, text=True,
                            check=False, timeout=timeout)
    if result.returncode != 0:
        raise ProvisionError(f"`{' '.join(args)}` failed: "
                             f"{(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def _reap_stale_databases(admin_dsn: str) -> None:
    """Drop the databases of sessions that are no longer running.

    A session drops its own database on the way out; one that is killed cannot. The
    owner's pid is in the name, so a leftover can be told from the database of a
    *concurrently running* suite, which must be left alone.
    """
    import psycopg  # noqa: PLC0415  -- absent psycopg is reported as a missing dep below

    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as conn:
            names = [r[0] for r in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s",
                (DATABASE_PREFIX + "%",)).fetchall()]
            for name in names:
                owner = name[len(DATABASE_PREFIX):].split("_")[0]
                if owner.isdigit() and not _pid_alive(int(owner)):
                    _drop_database(conn, name)
    except psycopg.Error:
        # Reaping is housekeeping: a server that will not answer it is a problem this
        # run is about to hit anyway, with a message that says so.
        pass


def _drop_database(conn, name: str) -> None:
    """Drop *name*, disconnecting whatever is still attached to it."""
    from psycopg import sql  # noqa: PLC0415

    conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                 "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _host_port(container: str) -> str:
    raw = _run("docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", container)
    ports = json.loads(raw)
    bindings = ports.get("5432/tcp") or []
    if not bindings:
        raise ProvisionError("the test Postgres container published no host port")
    return bindings[0]["HostPort"]


def _wait_until_ready(dsn: str) -> None:
    """Block until the server answers, or raise naming how long it was given."""
    import psycopg  # noqa: PLC0415  -- absent psycopg is reported as a missing dep below

    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as error:  # server still initialising
            last = error
            time.sleep(0.25)
    raise ProvisionError(
        f"the test Postgres ({IMAGE}) did not accept connections within "
        f"{_STARTUP_TIMEOUT_S:.0f}s: {last}")


class Session(NamedTuple):
    """What :func:`stop` needs to give a session's database back."""

    admin_dsn: str
    database: str


def _host_port_of(name: str) -> str | None:
    """The published port of the running server named *name*, or None if there is none."""
    try:
        state = _run("docker", "inspect", "-f", "{{.State.Running}}", name, timeout=30)
    except (ProvisionError, FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if state.strip() != "true":
        # Exited, or left behind by a reboot. Removing it is what lets the run below
        # start a healthy one under the same name.
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, check=False, timeout=60)
        return None
    try:
        return _host_port(name)
    except (ProvisionError, ValueError):
        return None


def _start_server() -> str:
    """Start the named server and return its published port.

    A throwaway database is worth no durability: fsync off and the data directory in
    tmpfs turn the ingest-heavy tests from disk-bound into memory-bound. No ``--rm``:
    the server outlives the session that started it, so the next one does not pay for
    it again.
    """
    try:
        _run("docker", "run", "-d",
             "--name", CONTAINER_NAME,
             "--label", f"{OWNER_LABEL}=1",
             "-e", f"POSTGRES_PASSWORD={_PASSWORD}",
             "-e", f"POSTGRES_DB={ADMIN_DATABASE}",
             "-e", "PGDATA=/pgdata",
             "--tmpfs", "/pgdata:rw",
             "-P", IMAGE,
             "postgres", "-c", "fsync=off", "-c", "full_page_writes=off",
             "-c", "synchronous_commit=off",
             timeout=300)
    except (ProvisionError, OSError, subprocess.SubprocessError) as error:
        # Two sessions starting together both find no server and both run this; the
        # loser is refused the name. That is the winner's server, which is exactly what
        # this one wanted, so it waits for it rather than reporting a failure.
        port = _host_port_of(CONTAINER_NAME)
        if port:
            return port
        raise ProvisionError(
            f"could not start the test Postgres from `{IMAGE}`. Pull it "
            f"(`docker pull {IMAGE}`) or set {DSN_ENV} to an existing database. "
            f"Underlying error: {error}") from error
    return _host_port(CONTAINER_NAME)


def _dsn(port: str, database: str) -> str:
    """Keyword/value form, not a URI.

    The tests select a schema by appending ``" options=-csearch_path=<schema>"`` to
    whatever this DSN is, and libpq rejects a space inside a URI.
    """
    return (f"host=127.0.0.1 port={port} user=postgres password={_PASSWORD} "
            f"dbname={database}")


def _create_session_database(admin_dsn: str) -> str:
    """Create this session's own database in the shared server and return its name."""
    import psycopg  # noqa: PLC0415
    from psycopg import sql  # noqa: PLC0415

    name = f"{DATABASE_PREFIX}{os.getpid()}_{int(time.time())}"
    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10) as conn:
        _drop_database(conn, name)
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return name


def start() -> tuple[str, Session | None]:
    """Return ``(dsn, session)``; *session* is ``None`` when an operator supplied the DSN.

    Raises :class:`ProvisionError` naming the missing piece when nothing can be
    provisioned -- the caller turns that into a skip, which is the one legitimate skip.
    """
    existing = os.environ.get(DSN_ENV)
    if existing:
        return existing, None

    try:
        import psycopg  # noqa: PLC0415
    except ImportError as error:
        raise ProvisionError(
            "psycopg is not installed, so the index tests cannot connect to any "
            "database. Install the test extra: `make venv` (or "
            "`pip install -e '.[test]'`).") from error

    # The server from an earlier run answers this without a daemon round trip beyond the
    # inspect, which is the whole point of keeping it: the common case is not a start.
    port = _host_port_of(CONTAINER_NAME)
    if not port:
        try:
            _run("docker", "version", "--format", "{{.Server.Version}}", timeout=30)
        except (ProvisionError, FileNotFoundError, OSError,
                subprocess.SubprocessError) as error:
            raise ProvisionError(
                "no usable Docker daemon, so the suite cannot start its own Postgres. "
                "Either install/start Docker (https://docs.docker.com/engine/install/) "
                f"and let it pull `{IMAGE}`, or point the suite at an existing database "
                f"with `export {DSN_ENV}='postgresql://user:pw@host:5432/dbname'`. "
                f"Underlying error: {error}") from error
        port = _start_server()

    admin_dsn = _dsn(port, ADMIN_DATABASE)
    _wait_until_ready(admin_dsn)
    _reap_stale_databases(admin_dsn)
    try:
        database = _create_session_database(admin_dsn)
    except psycopg.Error as error:
        # A skip, not a crash in pytest_configure: the caller turns a ProvisionError into
        # the one legitimate skip, and anything else here takes the whole run down before
        # a single test has been collected.
        raise ProvisionError(
            f"the test Postgres ({CONTAINER_NAME}) would not create this session's "
            f"database. Remove it (`docker rm -f {CONTAINER_NAME}`) and the next run "
            f"starts a fresh one, or set {DSN_ENV} to a database of your own. "
            f"Underlying error: {error}") from error
    return _dsn(port, database), Session(admin_dsn, database)


def stop(session: Session | None) -> None:
    """Drop this session's database. The server stays for the next run."""
    if not session:
        return
    import psycopg  # noqa: PLC0415

    try:
        with psycopg.connect(session.admin_dsn, autocommit=True,
                             connect_timeout=10) as conn:
            _drop_database(conn, session.database)
    except psycopg.Error:
        # The next run reaps it by owner pid, so a server that cannot be reached now
        # costs a database until then rather than leaking one forever.
        pass
