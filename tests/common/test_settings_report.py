# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The service's configuration read-back, and the two properties it must not lose.

The report enumerates the ENVIRONMENT rather than a hand-written catalogue, which is what
makes it complete: a setting somebody adds tomorrow appears without anyone updating a list.
The price is that an unrecognised key reaches the surface, so the classification has to fail
safe -- and both halves of that are what these tests pin.
"""

import json

import pytest

from robovast.service import settings_report
from robovast.service.settings_report import KNOWN, Sensitivity, describe

SENTINEL = "sentinel-value-that-must-never-be-served"


@pytest.fixture
def clean_env(monkeypatch):
    """A process environment holding no ROBOVAST_* but what a test puts there."""
    import os
    for key in [k for k in os.environ if k.startswith(settings_report.PREFIX)]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    # The git token has a second source -- its mount -- which a developer box does not have,
    # but point it somewhere certainly absent so the test does not depend on that.
    monkeypatch.setattr(settings_report, "GIT_TOKEN_MOUNT", "/nonexistent/robovast-git")
    return monkeypatch


def test_no_secret_value_ever_reaches_the_response(clean_env):
    """Every SECRET key set to a sentinel, and the sentinel nowhere in the payload.

    The one test this feature cannot pass halfway. It is written over the serialized
    response rather than field by field so that a value smuggled into a description, a
    default or a group is caught too -- the leak that a per-field assertion would miss.
    """
    secrets = [k for k, v in KNOWN.items() if v.sensitivity is Sensitivity.SECRET]
    assert secrets, "no secrets classified -- the test would pass vacuously"
    for key in secrets:
        clean_env.setenv(key, SENTINEL)

    payload = json.dumps([vars(r) for r in describe(loopback=True)])

    assert SENTINEL not in payload
    reported = {r.key: r for r in describe(loopback=True)}
    for key in secrets:
        assert reported[key].is_set, f"{key} should still be reported as set"
        assert reported[key].value is None
        assert reported[key].withheld == "secret"


def test_an_unknown_setting_is_reported_without_its_value(clean_env):
    """The fail-safe, and the property that lets the catalogue be incomplete.

    A key nobody has classified is IN FORCE, so hiding it would misreport the service. Its
    value is withheld anyway: the next setting somebody adds may be a credential, and it
    must not leak through a surface written before it existed.
    """
    clean_env.setenv("ROBOVAST_SOMETHING_NOBODY_CLASSIFIED", SENTINEL)

    rows = {r.key: r for r in describe(loopback=True)}
    row = rows["ROBOVAST_SOMETHING_NOBODY_CLASSIFIED"]

    assert row.is_set
    assert row.value is None
    assert row.withheld == "unclassified"
    assert SENTINEL not in json.dumps([vars(r) for r in rows.values()])


def test_the_report_covers_robovast_settings_and_nothing_else(clean_env):
    """Scope: process plumbing is out, and so is the operator's unrelated environment."""
    clean_env.setenv("ROBOVAST_TTY", "1")           # INTERNAL
    clean_env.setenv("AWS_SECRET_ACCESS_KEY", SENTINEL)   # not ours to report
    clean_env.setenv("ROBOVAST_NTFY_TOPIC", "campaigns")

    rows = {r.key: r for r in describe()}

    assert "ROBOVAST_TTY" not in rows
    assert "AWS_SECRET_ACCESS_KEY" not in rows
    assert rows["ROBOVAST_NTFY_TOPIC"].value == "campaigns"


def test_host_paths_follow_the_loopback_rule(clean_env):
    """The same rule ``/version`` applies to results_root: same-host callers only."""
    clean_env.setenv("ROBOVAST_WORKSPACES_ROOT", "/srv/robovast/workspaces")

    local = {r.key: r for r in describe(loopback=True)}["ROBOVAST_WORKSPACES_ROOT"]
    remote = {r.key: r for r in describe(loopback=False)}["ROBOVAST_WORKSPACES_ROOT"]

    assert local.value == "/srv/robovast/workspaces"
    assert local.withheld is None
    assert remote.is_set and remote.value is None
    assert remote.withheld == "host_path"


def test_registry_details_do_not_cross_the_interface(clean_env):
    """Set/not-set only, even for a loopback caller.

    Unlike a host path this is not about who is asking: registry endpoints and refs stay
    server-side for every caller (``RegistryConfig``, ``VersionInfo.build_unavailable``).
    """
    clean_env.setenv("ROBOVAST_REGISTRY_SERVER", SENTINEL)

    row = {r.key: r for r in describe(loopback=True)}["ROBOVAST_REGISTRY_SERVER"]

    assert row.is_set
    assert row.value is None
    assert row.withheld == "server_only"


def test_a_known_setting_that_is_unset_still_has_a_row(clean_env):
    """"ntfy is not configured" should be readable, not inferred from an absence."""
    rows = {r.key: r for r in describe()}

    assert rows["ROBOVAST_NTFY_TOPIC"].is_set is False
    assert rows["ROBOVAST_NTFY_TOPIC"].withheld is None
    # And where the reading code has a default, the row states it rather than leaving the
    # reader to guess that unset means off.
    assert rows["ROBOVAST_NTFY_SERVER"].default == "https://ntfy.sh"


def test_declared_defaults_match_what_the_reading_code_does(clean_env):
    """The defaults are imported from the read sites; this catches an import going stale."""
    from robovast.common.execution import default_image_project, default_image_tag

    rows = {r.key: r for r in describe()}

    assert rows["ROBOVAST_PROJECT"].default == default_image_project()
    assert rows["ROBOVAST_PROJECT_TAG"].default == default_image_tag()


def test_the_git_token_is_seen_through_its_mount(clean_env, tmp_path):
    """It is deliberately never an env var in the pod, so os.environ cannot answer for it."""
    assert {r.key: r for r in describe()}["ROBOVAST_GIT_TOKEN"].is_set is False

    mount = tmp_path / "token"
    mount.write_text("ghp_whatever")
    clean_env.setattr(settings_report, "GIT_TOKEN_MOUNT", str(mount))

    row = {r.key: r for r in describe()}["ROBOVAST_GIT_TOKEN"]
    assert row.is_set
    assert row.value is None
    assert row.withheld == "secret"


def test_how_to_change_names_the_command_this_deployment_needs(clean_env):
    """A pod needs a roll to re-read its Secrets; a local serve needs a restart."""
    assert "vast serve" in settings_report.how_to_change()

    clean_env.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert "vast service upgrade" in settings_report.how_to_change()
