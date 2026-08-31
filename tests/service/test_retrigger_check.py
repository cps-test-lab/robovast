# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The retrigger pre-flight: can this campaign be re-run, and if not, what is missing.

``prepare`` could only answer by *doing* it -- staging a tree, then raising with one reason at
a time -- so "is this worth trying?" cost a staging directory and told you about the config
without mentioning that the image was also gone. ``check`` walks the same records and reports
every axis at once.

Two properties are load-bearing and easy to lose:

* ``unknown`` must not block. Campaigns recorded before a field existed are exactly the ones
  this exists to rescue, so refusing them for lacking a record nobody wrote defeats the point.
* it must **spend nothing**. The first implementation read the legacy compat marker with
  ``docker run``, which *pulls* an absent image -- turning a pre-flight into a network fetch,
  and against an unreachable registry into a hang.
"""

import pathlib

import yaml

from robovast.service.retrigger import (AXIS_BLOCKED, AXIS_OK, AXIS_UNKNOWN, AXIS_UPGRADABLE,
                                        check)


def _campaign(tmp_path: pathlib.Path, *, config: dict | None = None,
              execution: dict | None = None, name: str = "c-2026-01-01-000000") -> pathlib.Path:
    """A campaign directory with only the records a test cares about."""
    root = tmp_path / name
    if config is not None:
        (root / "_config").mkdir(parents=True)
        (root / "_config" / "campaign.vast").write_text(yaml.safe_dump(config), encoding="utf-8")
    if execution is not None:
        (root / "_execution").mkdir(parents=True, exist_ok=True)
        (root / "_execution" / "execution.yaml").write_text(yaml.safe_dump(execution),
                                                            encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_a_campaign_with_no_frozen_config_is_blocked_and_says_where_to_go(tmp_path):
    report = check(_campaign(tmp_path), "c-2026-01-01-000000")
    assert report["axes"]["config"]["verdict"] == AXIS_BLOCKED
    assert "workspace it came from" in report["axes"]["config"]["detail"]
    assert report["runnable"] is False
    assert "config" in report["blocking"]


def test_a_current_config_is_ok(tmp_path):
    from robovast.common.migrations import SUPPORTED_CONFIG_VERSION

    root = _campaign(tmp_path, config={"version": SUPPORTED_CONFIG_VERSION, "execution": {}})
    axis = check(root, root.name)["axes"]["config"]
    assert axis["verdict"] == AXIS_OK
    assert axis["version"] == SUPPORTED_CONFIG_VERSION


def test_an_old_config_is_upgradable_and_names_the_steps(tmp_path):
    """Not blocked: the ladder carries it forward into the staging copy. The report says so
    explicitly, including that the archived file stays untouched -- which is the thing a user
    is right to worry about when told their config will be migrated."""
    root = _campaign(tmp_path, config={"version": 1, "execution": {"image": "img:1"}})
    axis = check(root, root.name)["axes"]["config"]
    assert axis["verdict"] == AXIS_UPGRADABLE
    assert axis["steps"] == ["1_to_2", "2_to_3"]
    assert "not modified" in axis["detail"]
    assert check(root, root.name)["runnable"] is True


def test_a_config_from_a_newer_robovast_is_blocked(tmp_path):
    from robovast.common.migrations import SUPPORTED_CONFIG_VERSION

    root = _campaign(tmp_path, config={"version": SUPPORTED_CONFIG_VERSION + 5, "execution": {}})
    axis = check(root, root.name)["axes"]["config"]
    assert axis["verdict"] == AXIS_BLOCKED
    assert "upgrade robovast" in axis["detail"]


def test_an_unreadable_config_is_diagnosed_not_raised(tmp_path):
    """The report must survive the file it is diagnosing. Reading through the strict loader
    would raise before the answer could be given, turning the diagnosis into the failure."""
    root = tmp_path / "c-2026-01-01-000000"
    (root / "_config").mkdir(parents=True)
    (root / "_config" / "campaign.vast").write_text("{{ not yaml", encoding="utf-8")
    axis = check(root, root.name)["axes"]["config"]
    assert axis["verdict"] == AXIS_BLOCKED
    assert "could not be read" in axis["detail"]


def test_missing_records_are_unknown_and_do_not_block(tmp_path):
    """The whole point. A campaign predating plugins.yaml/providers.yaml is the case this
    feature exists for; refusing it would be refusing the requirement."""
    from robovast.common.migrations import SUPPORTED_CONFIG_VERSION

    root = _campaign(tmp_path, config={"version": SUPPORTED_CONFIG_VERSION, "execution": {}})
    report = check(root, root.name)
    for axis in ("plugins", "providers"):
        assert report["axes"][axis]["verdict"] == AXIS_UNKNOWN
    assert report["runnable"] is True
    assert report["blocking"] == []


def test_recorded_plugins_and_providers_are_reported(tmp_path):
    from robovast.common.campaign_data import write_plugins_record, write_providers_record
    from robovast.common.migrations import SUPPORTED_CONFIG_VERSION

    root = _campaign(tmp_path, config={"version": SUPPORTED_CONFIG_VERSION, "execution": {}})
    write_plugins_record(root, {"scenario_mt": {"version": "1.4.2", "commit": "c" * 40}})
    write_providers_record(root, {"roqsim_assets": {"version": "0.1.0"}})
    report = check(root, root.name)
    assert report["axes"]["plugins"]["verdict"] == AXIS_OK
    assert report["axes"]["plugins"]["plugins"]["scenario_mt"]["commit"] == "c" * 40
    assert report["axes"]["providers"]["verdict"] == AXIS_OK


def test_a_plugin_resolved_nowhere_is_unknown_not_ok(tmp_path):
    """`resolved: false` means the code that ran came from a location this record cannot name,
    so reporting it as pinned would be a lie a re-run acts on."""
    from robovast.common.campaign_data import write_plugins_record
    from robovast.common.migrations import SUPPORTED_CONFIG_VERSION

    root = _campaign(tmp_path, config={"version": SUPPORTED_CONFIG_VERSION, "execution": {}})
    write_plugins_record(root, {"elsewhere": {"requested": "elsewhere", "resolved": False}})
    axis = check(root, root.name)["axes"]["plugins"]
    assert axis["verdict"] == AXIS_UNKNOWN
    assert "elsewhere" in axis["detail"]


def test_the_check_never_starts_or_pulls_a_container(tmp_path, monkeypatch):
    """It is a pre-flight: it must cost nothing.

    The first implementation read the legacy marker with ``docker run``, which pulls an absent
    image -- so checking a year-old campaign fetched gigabytes, or hung against a registry that
    could not serve it. Asserting on the argv is the only way to keep that from coming back.

    "Costs nothing" is about starting containers and moving layers, not about touching the
    network at all: a registry metadata read is how this question gets answered for an image
    that is not on this machine, and refusing it would leave the pre-flight unable to answer in
    exactly the case it exists for.
    """
    from robovast.common import execution
    from robovast.common.migrations import SUPPORTED_CONFIG_VERSION

    calls = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))

        class Result:
            returncode = 1
            stdout = ""
        return Result()

    monkeypatch.setattr(execution.subprocess, "run", fake_run)
    root = _campaign(tmp_path, config={"version": SUPPORTED_CONFIG_VERSION, "execution": {}},
                     execution={"execution_type": "local", "images": {"sut": "img:1"},
                                "image_revisions": {"sut": "sha256:abc"}})
    check(root, root.name)

    for args in calls:
        if args[:2] == ["docker", "run"]:
            assert "--pull=never" in args, f"a probe may not pull: {args}"
    # `buildx` is allowed, and is the point: `buildx imagetools inspect` reads an image's
    # config from the registry, starting nothing and fetching no layer. That is how a
    # pre-flight answers for an image this machine does not have -- which is the whole case
    # a year-old campaign presents, and what the local-daemon-only probe could not do.
    assert all(args[1] in ("inspect", "image", "run", "buildx") for args in calls), calls


def test_every_axis_is_reported_even_when_one_blocks(tmp_path):
    """Fixing one problem to discover the next is what this replaces, so a blocking axis must
    not short-circuit the others."""
    root = _campaign(tmp_path, config={"version": 999, "execution": {}})
    report = check(root, root.name)
    assert set(report["axes"]) == {"config", "host", "images", "plugins", "providers"}
    assert report["axes"]["config"]["verdict"] == AXIS_BLOCKED
    assert all(report["axes"][a]["detail"] for a in report["axes"]), "every axis needs a detail"


def test_a_version_1_campaign_can_be_prepared_at_all(tmp_path):
    """The headline: before this, prepare() refused every campaign older than the current
    config version, because it loaded the frozen .vast through the strict policy.

    Also pins the two consequences. The staged copy is migrated -- so `_builds_an_image` and
    stage_project read a shape they understand, rather than silently answering "builds nothing"
    for a v1 config that has no execution.containers at all. And the ARCHIVED copy is
    byte-identical afterwards, because it is the record of what its author wrote.
    """
    from robovast.service import retrigger

    class Request:  # the interface model, injected so this module never imports it
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            for field in ("config_filter", "campaign_name", "runs", "postprocess",
                          "upload_to_share", "show_gui", "description", "workspace_id",
                          "config_path"):
                self.__dict__.setdefault(field, None)

    source = _campaign(
        tmp_path,
        config={"version": 1, "metadata": {"name": "old"},
                "execution": {"image": "ghcr.io/x/y:1", "runs": 2,
                              "scenario_file": "scenario.osc"}},
        execution={"execution_type": "local", "robovast_version": "abc1234",
                   "images": {"scenario": "ghcr.io/x/y:1"},
                   "image_revisions": {"scenario": "ghcr.io/x/y@sha256:" + "a" * 64}})
    (source / "_config" / "scenario.osc").write_text("# scenario\n", encoding="utf-8")
    archived = (source / "_config" / "campaign.vast").read_text(encoding="utf-8")

    plan = retrigger.prepare(source, source.name, workspaces_root=tmp_path / "ws",
                             description_limit=200, request_model=Request)
    try:
        assert plan.config_migration == {"from": 1, "to": 3,
                                         "steps": ["1_to_2", "2_to_3"]}
        staged = yaml.safe_load(pathlib.Path(plan.config_path).read_text(encoding="utf-8"))
        assert staged["version"] == 3
        # v1's execution.image became a container, which is what the rest of prepare() reads.
        assert staged["execution"]["containers"]["scenario"]["image"] == "ghcr.io/x/y:1"
        assert (source / "_config" / "campaign.vast").read_text(encoding="utf-8") == archived
    finally:
        plan.discard()


def test_the_staged_migration_keeps_the_authors_comments(tmp_path):
    """Whoever opens the staged config to work out what it does needs the notes that explain
    it, and a migration is exactly when they will."""
    from robovast.common.migrations import SUPPORTED_CONFIG_VERSION
    from robovast.service import retrigger

    class Request:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            for field in ("config_filter", "campaign_name", "runs", "postprocess",
                          "upload_to_share", "show_gui", "description", "workspace_id",
                          "config_path"):
                self.__dict__.setdefault(field, None)

    source = tmp_path / "c-2026-01-01-000000"
    (source / "_config").mkdir(parents=True)
    (source / "_config" / "campaign.vast").write_text(
        "# why this campaign exists\n"
        "version: 1\n"
        "execution:\n"
        "  image: ghcr.io/x/y:1   # the one image\n"
        "  runs: 1\n"
        "  scenario_file: scenario.osc\n", encoding="utf-8")
    (source / "_config" / "scenario.osc").write_text("# scenario\n", encoding="utf-8")
    (source / "_execution").mkdir(parents=True)
    (source / "_execution" / "execution.yaml").write_text(
        yaml.safe_dump({"execution_type": "local", "images": {"scenario": "ghcr.io/x/y:1"},
                        "image_revisions": {"scenario": "ghcr.io/x/y@sha256:" + "a" * 64}}),
        encoding="utf-8")

    plan = retrigger.prepare(source, source.name, workspaces_root=tmp_path / "ws",
                             description_limit=200, request_model=Request)
    try:
        text = pathlib.Path(plan.config_path).read_text(encoding="utf-8")
        assert "# why this campaign exists" in text
        assert f"version: {SUPPORTED_CONFIG_VERSION}" in text
    finally:
        plan.discard()
