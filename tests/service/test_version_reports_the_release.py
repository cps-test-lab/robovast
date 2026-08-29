# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The service has to report the release it is, not only the commit it is.

``robovast_version`` is resolved by ``get_app_version``, which prefers a revision over
package metadata -- deliberately, so a long-lived service can be asked "is the fix I just
made loaded?". The consequence went unnoticed: a deployed image *always* bakes a revision
in (``container/*/Dockerfile``, guarded by ``test_release_bakes_revision``), so that
preference always fires, ``robovast_version`` and ``code_revision`` carried the same SHA on
every real deployment, and the semver in ``pyproject.toml`` was reported by nothing at all.

Two surfaces printed both fields side by side -- ``vast service info`` and the web UI's
Admin panel -- so both showed one string twice under two labels, and an operator asking
"which RoboVAST is this?" got a commit they could not look up in a changelog.

``package_version`` is the answer to that question. What is asserted here is that it stays
*independent*: it must keep saying the release when a revision is present (the deployed
shape, where the old fields cannot), and it must say nothing rather than borrow a revision
when there is no metadata -- the same refusal ``code_revision`` makes in the other
direction, and for the same reason.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from types import SimpleNamespace

from robovast.common.execution import GIT_REVISION_ENV
from robovast.service.local_transport import LocalTransport, _package_version

BAKED = "abc1234"


def _reported(monkeypatch, revision: str = BAKED):
    """What ``version()`` puts on the wire, with the deployed shape faked in.

    Calls the real method against a stub ``self`` rather than building a transport: the
    regression is in the *wiring* -- a field the model declares and the constructor call
    never passes -- so the method itself has to run.
    """
    monkeypatch.setenv(GIT_REVISION_ENV, revision)
    fake_self = SimpleNamespace(
        _campaigns_root=lambda: "/srv/campaigns",
        store=SimpleNamespace(registry=SimpleNamespace(root="/srv/sources")),
        _declared_web_base=lambda: "",
    )
    return LocalTransport.version(fake_self)


def test_the_release_is_reported_where_a_revision_exists(monkeypatch):
    """The load-bearing one, and exactly the deployed shape: a baked revision takes over
    the other two fields, and the semver still has to arrive."""
    info = _reported(monkeypatch)
    assert info.package_version == pkg_version("robovast")
    assert info.robovast_version == BAKED, "the handshake field still prefers the revision"
    assert info.code_revision == BAKED


def test_the_release_is_not_the_revision(monkeypatch):
    """The bug stated directly: before this field, every value on the panel was the SHA."""
    info = _reported(monkeypatch)
    assert info.package_version != info.code_revision, (
        "package_version collapsed back onto the revision -- the surfaces that print both "
        "are showing one string twice again")


def test_no_metadata_reports_nothing_rather_than_a_revision(monkeypatch):
    """A source tree with no metadata must not have a SHA substituted for its release.
    Reporting one would read as a version that was never cut, which is worse than the
    empty string -- the same trap ``code_revision`` refuses in the other direction."""
    def _absent(_name):
        raise PackageNotFoundError("robovast")

    monkeypatch.setattr("robovast.service.local_transport._pkg_version", _absent)
    assert _package_version() == ""
    assert _reported(monkeypatch).package_version == ""


def test_the_field_survives_a_service_that_never_sets_it():
    """It defaults to ``""`` so an older service, or any other implementation of the
    interface, stays constructible -- the field is additive, not a new requirement."""
    from robovast.service.interface import VersionInfo
    assert VersionInfo(robovast_version="x").package_version == ""
