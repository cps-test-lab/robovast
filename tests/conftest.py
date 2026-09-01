# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""RoboVAST is standalone: its suite must be green with no simulator installed.

``pip install robovast`` deliberately names no simulator — a backend arrives through its
own package and registers a ``robovast.simulators`` entry point (``roqsim`` is one, and a
third-party one would arrive the same way). That is a real claim about the packaging, and
the honest way to keep it true is for the tests to *demonstrate* it rather than assert it
in prose.

So the tests that genuinely need a registered backend are marked ``requires_simulator``
and **skip** when none is installed. The alternative — which is what happened — is ~25
tests failing with "Unknown robovast.simulators plugin", an incomplete environment
reported as broken code, sending the reader to the wrong file entirely.

``make venv`` installs ``src/robovast_sim_roqsim``, so a developer sees them run; they
skip only where robovast really is standing alone.
"""

import os
from importlib.metadata import entry_points

import pytest

#: Entry-point group a simulator backend registers itself in.
SIMULATOR_GROUP = "robovast.simulators"


@pytest.fixture(autouse=True)
def _isolated_login_config(tmp_path, monkeypatch):
    """No test may read or write the developer's real ``vast login``.

    ``detected_service_url()`` resolves local probe -> stored login, so any test that
    touches it silently depends on whether the person running the suite happens to be
    logged in. Three tests in ``test_service_target.py`` asserted "nothing answers, so
    the target is empty" and began failing the moment a real login existed -- a green
    suite on a fresh checkout, red on the maintainer's machine, for no change in the
    code.

    ``test_login.py`` had this fixture locally, which is exactly why the gap survived:
    per-module isolation protects the module that thought of it and nothing else. It
    belongs here, where it covers every test that will ever reach for a credential.
    """
    from robovast.client import login  # pylint: disable=import-outside-toplevel
    monkeypatch.setenv(login.CONFIG_ENV_VAR, str(tmp_path / "robovast-login.json"))


@pytest.fixture(autouse=True)
def _isolated_environment():
    """No test may leak the developer's ``./.env`` into another.

    Same shape as the login isolation above, and found the same way. The CLI reads
    ``./.env`` once per invocation, so any test that invokes a command loads the
    maintainer's real registry, share and ntfy settings into ``os.environ`` -- where they
    stay, for every test that runs afterwards in the same process.

    That is not hypothetical: with a ``ROBOVAST_REGISTRY_*`` block in the checkout's
    ``.env``, seven ``test_service_deploy`` assertions failed in a full run and passed in
    isolation, because ``deploy_service`` had built registry Secrets they never asked for.
    The suite had been green only because a stale entry point meant ``.env`` was silently
    never read at all.

    Snapshot-and-restore rather than stripping ``ROBOVAST_*``: tests that set these
    deliberately must keep working, and the leak is the *persistence*, not the value.
    """
    import os  # pylint: disable=import-outside-toplevel
    before = dict(os.environ)
    yield
    if os.environ != before:
        os.environ.clear()
        os.environ.update(before)


class _ClusterAccessInTest(BaseException):
    """A test reached a Kubernetes API server.

    Derived from ``BaseException`` rather than ``Exception``, deliberately. The paths this
    guards are reporting and best-effort steps that catch ``Exception`` broadly and degrade
    -- which is right in production and would, here, turn the guard into a silent no-op:
    the suite would be fast again and nobody would learn that the test never exercised the
    call at all. ``BaseException`` walks straight out through those handlers, the same way
    ``KeyboardInterrupt`` does, and pytest reports it against the test that caused it.
    """


@pytest.fixture(autouse=True)
def _no_test_reaches_a_real_cluster(request):
    """No test may send a request to a Kubernetes API server.

    Same shape as the login and ``.env`` isolation above, and found the same way -- except
    that this one had already gone wrong five times in a single fixture. Read
    ``tests/execution/test_cluster_setup_projectless.py``: every stub in it that says "an
    unstubbed call therefore reaches a real API server" is a step someone added to
    ``setup_server`` and a test author later discovered by watching the suite crawl. The
    invariant was maintained by hand, per call site, by whoever noticed.

    The cost is not only time, though the time is real: two unstubbed reads in
    ``setup_server`` took ONE of those tests from 0.02s to 80s, and the
    ``tests/execution`` + ``tests/common`` suite from 94 seconds to twenty minutes. The
    worse half is silent: the request goes to whatever the developer's current kubeconfig
    context names. Where that is unreachable the test merely waits out a connect timeout;
    where it is a **live cluster** the test quietly reads -- or writes -- the real one, and
    a suite that passes on a laptop with no cluster starts doing something else entirely on
    the machine that has one.

    Guarding the *transport* rather than the config loader is what makes it unescapable.
    ``kube_client.load_kube_config`` is the only sanctioned entry (enforced by
    ``test_kube_loader_is_the_only_entry``), but three ``cluster_config`` providers bind it
    at module import, so patching that name would miss them -- and would miss the fourth
    one somebody adds next year. Every path, however it loaded its config, ends at one
    method to send a request.

    It does not cover ``kubernetes.stream``'s websockets (pod exec/attach), which build
    their own connection a layer above this one. Nothing reaches one today -- every exec
    path is stubbed at ``kube_client.exec_stream`` -- so the gap is recorded rather than
    plugged, because a guard written for a case nobody has is a guess about what it
    should say.

    A test that means to talk to a cluster marks itself ``reaches_a_cluster``. Nothing does
    today, and the marker exists so that adding one is a deliberate line in a diff.
    """
    if "reaches_a_cluster" in request.keywords:
        yield
        return
    try:
        from kubernetes.client import rest  # pylint: disable=import-outside-toplevel
    except ImportError:
        # Core-only environment (no robovast_cluster, no kubernetes package): there is
        # nothing here that could reach a cluster, and the suite must still run.
        yield
        return

    original = rest.RESTClientObject.request

    def _refuse(self, method, url, *args, **kwargs):
        raise _ClusterAccessInTest(
            f"{request.node.nodeid} sent a {method} to a Kubernetes API server.\n"
            "Tests must not reach a cluster: off a cluster this waits out a connect "
            "timeout (~80s per call), and on a machine that HAS one it reads or writes "
            "the real thing.\n"
            "Stub the step that made the call -- the failing traceback names it -- or, "
            "if the call is genuinely the point, mark the test 'reaches_a_cluster'.")

    rest.RESTClientObject.request = _refuse
    try:
        yield
    finally:
        rest.RESTClientObject.request = original


def simulator_backends() -> list[str]:
    """Registered simulator backends, by name."""
    return sorted(e.name for e in entry_points(group=SIMULATOR_GROUP))


def pytest_collection_modifyitems(config, items):
    if simulator_backends():
        return
    skip = pytest.mark.skip(
        reason=f"no {SIMULATOR_GROUP} backend installed — robovast is standalone, so "
               "these skip rather than fail. `make venv` (or `pip install -e "
               "src/robovast_sim_roqsim`) installs one.")
    for item in items:
        if "requires_simulator" in item.keywords:
            item.add_marker(skip)


# --- the suite's own Postgres -------------------------------------------------------
#
# The index tests are gated on ``ROBOVAST_TEST_PG_DSN``; unset, ~240 of them skipped and
# the migration's whole correctness coverage silently did not run. The suite now provides
# the database itself (see ``tests/pg_provision``), so no test depends on a service a
# human started. Started once here and torn down once in ``pytest_unconfigure``: 240 tests
# must not pay per-test container startup.
#
# This must happen in ``pytest_configure`` rather than a session fixture -- the gates are
# module-level ``skipif``/``os.environ.get`` evaluated at import time, i.e. during
# collection, by which point any fixture has not run yet.

_pg_container = None
_pg_unavailable: str | None = None


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_simulator: needs a registered robovast.simulators backend "
        "(installed by `make venv`); skipped when robovast stands alone")
    config.addinivalue_line(
        "markers",
        "reaches_a_cluster: deliberately sends requests to a Kubernetes API server; "
        "exempt from the guard in _no_test_reaches_a_real_cluster")

    global _pg_container, _pg_unavailable  # pylint: disable=global-statement
    from tests import pg_provision  # pylint: disable=import-outside-toplevel

    if config.option.collectonly:
        return
    try:
        dsn, _pg_container = pg_provision.start()
    except pg_provision.ProvisionError as error:
        _pg_unavailable = str(error)
        return
    os.environ[pg_provision.DSN_ENV] = dsn


def pytest_unconfigure(config):  # pylint: disable=unused-argument
    from tests import pg_provision  # pylint: disable=import-outside-toplevel
    pg_provision.stop(_pg_container)


def pytest_report_header(config):  # pylint: disable=unused-argument
    if _pg_unavailable:
        return f"postgres: NOT provisioned -- {_pg_unavailable}"
    if _pg_container:
        return "postgres: provisioned by the suite (throwaway container)"
    return "postgres: ROBOVAST_TEST_PG_DSN was set; using it as-is"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):  # pylint: disable=unused-argument
    """Give the one legitimate skip a reason that names what is missing.

    The gates live in 26 files and phrase themselves as "ROBOVAST_TEST_PG_DSN is not
    set", which tells the reader an env var is unset but not that the suite tried and
    failed to provision a database, nor how to get one. Rewriting the reason here fixes
    every one of them -- including the in-body ``pytest.skip`` calls a marker rewrite
    could not reach -- in a single place.
    """
    outcome = yield
    report = outcome.get_result()
    if not (_pg_unavailable and report.skipped):
        return
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3 \
            and "ROBOVAST_TEST_PG_DSN" in str(longrepr[2]):
        path, lineno, _ = longrepr
        report.longrepr = (path, lineno, f"Skipped: {_pg_unavailable}")
