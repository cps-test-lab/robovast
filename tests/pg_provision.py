# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Provision the throwaway Postgres the index tests run against.

The results-storage migration replaced the per-campaign SQLite ``data.db`` with a
central Postgres index, and its ~240 tests were gated on ``ROBOVAST_TEST_PG_DSN``.
With no database the suite reported "4875 passed, 300 skipped" -- green, while the
entire correctness coverage of the migration had not run. A suite that quietly skips
its most important tests is worse than one that fails, so the database is now the
suite's own responsibility rather than an operator's.

Mechanism: the ``docker`` CLI plus the ``psycopg`` that is already a test dependency.
``pytest-postgresql`` and ``testcontainers`` would each add a dependency (and
``pytest-postgresql`` additionally needs a local ``initdb``/``pg_ctl``, which this
environment does not have) to do the same thing; the CLI needs nothing new, and
``robovast_client`` keeps its empty dependency set.

Timing: this runs from ``pytest_configure``, *not* a session fixture, because the
gates are module-level -- ``DSN = os.environ.get(...)`` and ``pytest.mark.skipif`` are
evaluated at import time, which is collection, which is after every fixture would be
too late. ``pytest_configure`` is the last hook that still precedes collection.

Isolation: the container gets a random host port (``-P``) rather than a fixed one, so
concurrent suites -- and any Postgres already running on this host -- do not collide.
Each test file still creates and drops its own schema; this only supplies the server.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

#: The env var the tests read. Set by the caller, honoured when already set.
DSN_ENV = "ROBOVAST_TEST_PG_DSN"

#: Small, cached, and Postgres 16 like the deployment. Pinned: a test database that
#: drifts with :latest turns an upstream release into a mystery failure here.
IMAGE = "postgres:16-alpine"

#: Marks our containers so a crashed run's leftovers can be identified and reaped.
OWNER_LABEL = "robovast-test-pg"

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


def _reap_orphans() -> None:
    """Remove containers left by a run that was killed before its teardown.

    ``--rm`` covers a container that exits; it does not cover a pytest that is SIGKILLed
    while the container is healthy. The owner PID is carried as a label so a leftover can
    be told from the container of a *concurrently running* suite, which must be left
    alone.
    """
    try:
        ids = _run("docker", "ps", "-q", "--filter", f"label={OWNER_LABEL}=1").split()
    except (ProvisionError, OSError, subprocess.SubprocessError):
        return
    for container in ids:
        try:
            owner = _run("docker", "inspect", "-f",
                         f"{{{{index .Config.Labels \"{OWNER_LABEL}-owner\"}}}}",
                         container)
            if owner and not _pid_alive(int(owner)):
                subprocess.run(["docker", "rm", "-f", container],
                               capture_output=True, check=False, timeout=60)
        except (ProvisionError, OSError, ValueError, subprocess.SubprocessError):
            continue


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


def _wait_until_ready(dsn: str, container: str) -> None:
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


def start() -> tuple[str, str | None]:
    """Return ``(dsn, container_id)``; ``container_id`` is ``None`` when reusing.

    Raises :class:`ProvisionError` naming the missing piece when nothing can be
    provisioned -- the caller turns that into a skip, which is the one legitimate skip.
    """
    existing = os.environ.get(DSN_ENV)
    if existing:
        return existing, None

    try:
        import psycopg  # noqa: F401,PLC0415
    except ImportError as error:
        raise ProvisionError(
            "psycopg is not installed, so the index tests cannot connect to any "
            "database. Install the test extra: `make venv` (or "
            "`pip install -e '.[test]'`).") from error

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

    _reap_orphans()

    # A throwaway database is worth no durability: fsync off and the data directory in
    # tmpfs turn the ingest-heavy tests from disk-bound into memory-bound.
    try:
        container = _run(
            "docker", "run", "-d", "--rm",
            "--label", f"{OWNER_LABEL}=1",
            "--label", f"{OWNER_LABEL}-owner={os.getpid()}",
            "-e", "POSTGRES_PASSWORD=robovast-test",
            "-e", "POSTGRES_DB=robovast_test",
            "-e", "PGDATA=/pgdata",
            "--tmpfs", "/pgdata:rw",
            "-P", IMAGE,
            "postgres", "-c", "fsync=off", "-c", "full_page_writes=off",
            "-c", "synchronous_commit=off",
            timeout=300)
    except (ProvisionError, OSError, subprocess.SubprocessError) as error:
        raise ProvisionError(
            f"could not start the test Postgres from `{IMAGE}`. Pull it "
            f"(`docker pull {IMAGE}`) or set {DSN_ENV} to an existing database. "
            f"Underlying error: {error}") from error

    port = _host_port(container)
    # Keyword/value form, not a URI: the tests select a schema by appending
    # " options=-csearch_path=<schema>" to whatever this DSN is, and libpq rejects a
    # space inside a URI.
    dsn = ("host=127.0.0.1 port=%s user=postgres password=robovast-test "
           "dbname=robovast_test" % port)
    try:
        _wait_until_ready(dsn, container)
    except ProvisionError:
        stop(container)
        raise
    return dsn, container


def stop(container: str | None) -> None:
    if not container:
        return
    subprocess.run(["docker", "rm", "-f", container],
                   capture_output=True, check=False, timeout=120)
