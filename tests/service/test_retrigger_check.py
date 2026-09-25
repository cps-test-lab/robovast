# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The retrigger pre-flight: can this campaign be re-run, and if not, what is missing.

``check`` reports every axis at once, and ``unknown`` does not block: a campaign recorded
before a field existed is refused only for what it records, not for what it lacks.
"""

import pathlib

import pytest
import yaml

from robovast.service import retrigger
from robovast.service.retrigger import AXIS_BLOCKED, AXIS_OK, AXIS_UNKNOWN, AXIS_UPGRADABLE


def check(root, name, *, labels=None, lock=None):
    """``retrigger.check`` against a registry that answers with *labels* and *lock*."""
    return retrigger.check(root, name, image_labels=lambda _ref: labels,
                           build_lock=lambda _ref: lock or {})


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
    assert axis["steps"] == ["1_to_2", "2_to_3", "3_to_4", "4_to_5", "5_to_6"]
    assert "not modified" in axis["detail"]
    assert check(root, root.name)["runnable"] is True


def test_a_config_from_a_newer_robovast_is_blocked(tmp_path):
    from robovast.common.migrations import SUPPORTED_CONFIG_VERSION

    root = _campaign(tmp_path, config={"version": SUPPORTED_CONFIG_VERSION + 5, "execution": {}})
    axis = check(root, root.name)["axes"]["config"]
    assert axis["verdict"] == AXIS_BLOCKED
    assert "upgrade robovast" in axis["detail"]


@pytest.mark.parametrize("config,says", [
    ({"execution": {}}, "declares no 'version:'"),
    ({"version": "4", "execution": {}}, "must be an integer"),
])
def test_a_config_with_no_usable_version_is_blocked_and_says_which(tmp_path, config, says):
    """There is nothing to start the ladder from, so a re-run cannot read this config at all.
    The detail has to name that; a verdict about how the version compares to the supported one
    describes an ordering the file does not have."""
    root = _campaign(tmp_path, config=config)
    axis = check(root, root.name)["axes"]["config"]
    assert axis["verdict"] == AXIS_BLOCKED
    assert says in axis["detail"]
    assert check(root, root.name)["runnable"] is False


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


def test_the_host_axis_answers_from_the_registry_labels(tmp_path):
    """The recorded image's protocol is read from its registry labels; an image the registry
    would not answer for is unknown and says why, and does not block."""
    from robovast.common.execution import COMPAT_VERSION, COMPAT_VERSION_LABEL
    from robovast.common.migrations import SUPPORTED_CONFIG_VERSION

    root = _campaign(tmp_path, config={"version": SUPPORTED_CONFIG_VERSION, "execution": {}},
                     execution={"images": {"sut": "reg.example/img:1"},
                                "image_revisions": {"sut": "reg.example/img@sha256:" + "a" * 64}})
    host = check(root, root.name,
                 labels={COMPAT_VERSION_LABEL: str(COMPAT_VERSION)})["axes"]["host"]
    assert host["verdict"] == AXIS_OK

    report = check(root, root.name, labels=None)
    assert report["axes"]["host"]["verdict"] == AXIS_UNKNOWN
    assert "registry would not answer" in report["axes"]["host"]["detail"]
    assert "host" not in report["blocking"]


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

    class Request:  # the interface model, injected so this module never imports it
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            for field in ("config_filter", "campaign_name", "runs", "postprocess",
                          "upload_to_share", "description", "workspace_id",
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
        assert plan.config_migration == {"from": 1, "to": 6,
                                         "steps": ["1_to_2", "2_to_3", "3_to_4", "4_to_5",
                                                   "5_to_6"]}
        staged = yaml.safe_load(pathlib.Path(plan.config_path).read_text(encoding="utf-8"))
        assert staged["version"] == 6
        # v1's execution.image became a container, which is what the rest of prepare() reads.
        assert staged["execution"]["containers"]["scenario"]["image"] == "ghcr.io/x/y:1"
        assert (source / "_config" / "campaign.vast").read_text(encoding="utf-8") == archived
    finally:
        plan.discard()


def test_the_staged_migration_keeps_the_authors_comments(tmp_path):
    """Whoever opens the staged config to work out what it does needs the notes that explain
    it, and a migration is exactly when they will."""
    from robovast.common.migrations import SUPPORTED_CONFIG_VERSION

    class Request:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            for field in ("config_filter", "campaign_name", "runs", "postprocess",
                          "upload_to_share", "description", "workspace_id",
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
