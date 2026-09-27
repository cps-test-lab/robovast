# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast doctor``'s service rows, which need no kubectl: the handshake, the revision the
service runs, and whether it can build images.

``image builds`` reports what the running service says it can do; the cluster plugin's
``build registry`` and ``registry route`` describe the infrastructure. They can legitimately
disagree — a pod predating its own config — which is why they carry different names.
"""

# pylint: disable=protected-access  # the row helpers are what this file tests

import types
from unittest.mock import MagicMock, patch

from robovast.client import doctor as doc


def _handshake(version=None, raises=None):
    """The service's VersionInfo as `check_client` reads it, with the HTTP call stubbed."""
    client = MagicMock()
    if raises is not None:
        client.version.side_effect = raises
    else:
        client.version.return_value = version
    with patch("robovast.service.http_client.RobovastClient", return_value=client):
        return doc._service_version("https://svc.example")  # noqa: SLF001


def _client_checks(version=None, raises=None):
    """`check_client`'s build line, with the handshake stubbed.

    Splatted, because the handshake answers ``(info, error)``: the error is returned rather
    than swallowed so that a service which ANSWERED and refused can be told apart from one
    nothing replied to. Passing the pair as ``info`` alone is not a type error -- a tuple has
    no ``code_revision``, so every case reads as "the service did not report one" and the
    branch under test is never reached.
    """
    return doc._check_build_capability(*_handshake(version, raises))  # noqa: SLF001


def _revision_checks(version=None, raises=None, here="abc1234"):
    """`check_client`'s revision line, with the handshake and this side's revision stubbed."""
    with patch("robovast.client.app_version.running_revision", return_value=here):
        return doc._check_service_revision(*_handshake(version, raises))  # noqa: SLF001


def test_a_service_that_gave_no_verdict_produces_no_line():
    """`None` is "did not say". A service older than the field must not be told to fix
    itself."""
    from robovast.service.interface import VersionInfo

    assert _client_checks(VersionInfo(robovast_version="2.0.0")) == []


def test_a_capable_service_reports_available():
    from robovast.service.interface import VersionInfo

    checks = _client_checks(VersionInfo(robovast_version="2.0.0", can_build_images=True))
    assert len(checks) == 1
    assert checks[0].ok is True


def test_an_incapable_service_carries_its_reason():
    from robovast.service.interface import VersionInfo

    checks = _client_checks(VersionInfo(
        robovast_version="2.0.0", can_build_images=False,
        build_unavailable="nowhere to push it. … 'vast service upgrade' …"))
    assert checks[0].ok is False
    assert "upgrade" in checks[0].fix
    assert checks[0].optional, "a service without a registry is not a broken install"


def test_an_unreadable_handshake_is_silent_rather_than_red():
    """A local `vast serve` whose token differs from the stored login answers 401. Turning
    that into a red line reports doctor's own credential mismatch as the service's fault."""
    assert _client_checks(raises=RuntimeError("401 Unauthorized")) == []


# -- which code is the service running --------------------------------------
#
# A service that never reloaded an edit makes every symptom read as a bug in the change.
# `vast --version` answers it for this side; this is the other side.


def _version(**kwargs):
    from robovast.service.interface import VersionInfo

    return VersionInfo(robovast_version="2.0.0", **kwargs)


def test_a_matching_revision_is_green_and_says_so():
    checks = _revision_checks(_version(code_revision="abc1234"), here="abc1234")
    assert len(checks) == 1
    assert checks[0].ok is True
    assert "abc1234" in checks[0].detail


def test_a_differing_revision_warns_and_names_the_roll():
    """Advisory, not fatal: being pointed at a deployment other than your own tree is
    normal, and a doctor that exited non-zero for it would be crying wolf."""
    checks = _revision_checks(_version(code_revision="abc1234"), here="def5678")
    assert checks[0].ok is False
    assert checks[0].optional
    assert "abc1234" in checks[0].detail and "def5678" in checks[0].detail
    assert "upgrade" in checks[0].fix


def test_no_reported_revision_is_distinguishable_from_a_mismatch():
    """"Cannot tell" must not be reported as "different code", which sends someone
    re-releasing a current service."""
    checks = _revision_checks(_version(code_revision=""), here="abc1234")
    assert checks[0].ok is False
    assert "not reported" in checks[0].detail
    assert "def5678" not in checks[0].detail
    assert "release-images" in checks[0].fix


def test_nothing_to_compare_against_is_not_a_mismatch():
    """A client-only or non-git install has no revision of its own. Reporting the service's
    and stopping beats inventing a comparison."""
    checks = _revision_checks(_version(code_revision="abc1234"), here="")
    assert checks[0].ok is True
    assert "abc1234" in checks[0].detail


def test_two_dirty_trees_are_not_claimed_to_match():
    """`+dirty` records that a tree was unclean; it cannot tell two unclean trees apart, so
    equality here is not proof and must not read as it."""
    checks = _revision_checks(_version(code_revision="abc1234+dirty"), here="abc1234+dirty")
    assert checks[0].ok is True
    assert "dirty" in checks[0].detail.lower()


def test_the_same_commit_abbreviated_differently_is_not_a_mismatch():
    """The two sides abbreviate independently -- the deployment's sha is baked into the
    image, this one comes from the local checkout -- so the same commit routinely arrives
    as `a9c955a` and `a9c955a7`, and must not read as a mismatch."""
    checks = _revision_checks(_version(code_revision="a9c955a"), here="a9c955a7")
    assert checks[0].ok is True
    assert "roll" not in (checks[0].fix or "").lower()

    # ...and the other way round, since which side is shorter is not fixed either.
    assert _revision_checks(_version(code_revision="a9c955a7"), here="a9c955a")[0].ok is True


def test_a_dirty_checkout_on_the_deployed_commit_is_not_reported_as_a_different_one():
    """The case a dirty working tree produces constantly. It is the same commit, so the row
    must not read `a9c955a deployed, a9c955a7+dirty here` -- which looks like two commits --
    while still refusing to claim the code matches, because it may well not."""
    checks = _revision_checks(_version(code_revision="a9c955a"), here="a9c955a7+dirty")
    assert checks[0].ok is True
    assert "same commit" in checks[0].detail
    assert "dirty" in checks[0].detail.lower()


def test_a_genuinely_different_commit_still_warns():
    """The prefix comparison must not make every revision look equal: a real mismatch is
    what this row exists to catch."""
    checks = _revision_checks(_version(code_revision="abc1234"), here="def5678")
    assert checks[0].ok is False


def test_a_truncated_revision_does_not_match_everything():
    """A prefix test is only as trustworthy as the shorter string. Git never abbreviates
    below 4, so anything shorter is malformed rather than short and must not be treated as
    naming a commit -- otherwise an empty-ish value would match every deployment."""
    checks = _revision_checks(_version(code_revision="abc1234"), here="a")
    assert checks[0].ok is False


def test_neither_side_having_one_says_nothing():
    """A client-only install talking to an older service: the remedy for "cannot report" is
    a re-release, which is not this user's job, and the service may be current anyway."""
    assert _revision_checks(_version(code_revision=""), here="") == []


def test_an_unreachable_service_says_nothing_about_the_revision():
    """Silent only when nothing replied: that fault belongs to the `service` row alone.

    A bare exception with no HTTP status is a request that never got an answer -- DNS, TCP,
    TLS, a timeout, a pod mid-roll. The `service` row is red and names it, and a second red
    line for the same fault sends a reader chasing it twice.
    """
    assert _revision_checks(raises=RuntimeError("connection refused")) == []


def test_a_refused_handshake_says_which_credential_fixes_it():
    """Answered-and-refused is a DIFFERENT fault, and it must not vanish.

    A service whose token does not match this deployment answers 401. That is not "no
    revision question exists", it is "your credentials cannot ask it" -- and a row that
    stays silent for it leaves the reader nothing to report at all.
    """
    checks = _revision_checks(raises=_Refused(401, "token does not match"))

    assert len(checks) == 1
    assert checks[0].ok is False and checks[0].optional
    assert "401" in checks[0].detail
    assert "vast login" in checks[0].fix


def test_a_refused_handshake_also_speaks_on_the_build_line():
    """Same rule, same reason, on the row that answers "can this service build?"."""
    checks = _client_checks(raises=_Refused(403, "forbidden"))

    assert len(checks) == 1
    assert checks[0].ok is False


def _service_row(target, err, monkeypatch):
    """``check_client``'s `service` row, with the handshake and the PATH probe stubbed."""
    from robovast.client import login as login_config
    from robovast.client import service_target

    monkeypatch.setattr(login_config, "credentials",
                        lambda: (target, "tok", "me") if target else ("", "", ""))
    monkeypatch.setattr(service_target, "detected_service_url", lambda: target)
    monkeypatch.setattr(doc, "_service_version", lambda t: (None, err))
    monkeypatch.setattr(doc, "_check_service_revision", lambda *a: [])
    monkeypatch.setattr(doc, "_check_build_capability", lambda *a: [])
    monkeypatch.setattr(doc.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""))
    return next(c for c in doc.check_client() if c.name == "service")


def test_the_service_row_is_green_only_when_the_service_answered(monkeypatch):
    """A stored login is configuration, not reachability."""
    row = _service_row("https://svc.example", None, monkeypatch)
    assert row.ok is True


def test_a_configured_but_silent_service_is_red_and_says_where_to_look(monkeypatch):
    row = _service_row("https://svc.example", RuntimeError("timed out"), monkeypatch)

    assert row.ok is False
    assert "not answering" in row.detail
    assert "mid-roll" in row.fix or "upgrade" in row.fix


def test_a_service_that_answered_and_refused_is_not_the_service_rows_fault(monkeypatch):
    """401 means it was reached, parsed the request and refused it.

    Only a request that never got an answer belongs on this row; the refusal is reported by
    the rows that own it, which name the credential that fixes it. Reading a 401 as "not
    answering" would send someone restarting a service that is up and working.
    """
    row = _service_row("https://svc.example", _Refused(401, "bad token"), monkeypatch)
    assert row.ok is True


class _Refused(Exception):
    """A service that answered and refused. ``status`` is what makes "it answered" decidable."""

    def __init__(self, status, detail=""):
        super().__init__(detail or f"HTTP {status}")
        self.status = status
        self.detail = detail
