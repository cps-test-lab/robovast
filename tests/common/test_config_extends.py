# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``extends:`` — what a campaign built on another one resolves to, and what is refused."""

import pytest

from robovast.common.common import load_config
from robovast.common.config_extends import extends_sources, resolve_extends

CAMPAIGN = """\
version: 4
execution:
  containers: {scenario: {image: a}}
  runs: 1
"""
BODY = "execution:\n  containers: {scenario: {image: a}}\n  runs: 1\n"


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _load(tmp_path, child, *, base=None):
    if base is not None:
        _write(tmp_path, "base.vast", base)
    return load_config(str(_write(tmp_path, "campaign.vast", child)))


# -- opting out --------------------------------------------------------------------

def test_a_config_without_extends_is_returned_untouched():
    """The whole existing corpus takes this path; passing through must not rewrite it."""
    config = {"version": 4, "execution": {"runs": 1}}
    assert resolve_extends(config, "/nowhere/x.vast") is config


def test_a_campaign_without_extends_lists_only_itself(tmp_path):
    path = _write(tmp_path, "campaign.vast", CAMPAIGN)
    assert extends_sources({"version": 4}, str(path)) == [path]


def test_every_shipped_vast_passes_through_the_expander_unchanged():
    """Opting out has to cost nothing, measured against the corpus, not one fixture."""
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[2]
    checked = 0
    for vast in sorted((root / "configs" / "examples").rglob("*.vast")):
        raw = yaml.safe_load(vast.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "extends" in raw:
            continue
        assert resolve_extends(raw, str(vast)) is raw, vast
        assert extends_sources(raw, str(vast)) == [vast]
        checked += 1
    assert checked > 10, f"expected the example corpus to be checked, saw {checked}"


# -- merging -----------------------------------------------------------------------

def test_mappings_merge_at_every_depth_and_the_child_wins(tmp_path):
    cfg = _load(
        tmp_path,
        "version: 4\nextends: base.vast\nexecution:\n  containers:\n"
        "    sut: {resources: {cpu: 8}}\n",
        base="execution:\n  containers:\n    scenario: {image: a}\n"
             "    sut: {image: s, resources: {cpu: 2, memory: 4Gi}}\n  runs: 1\n")
    sut = cfg["execution"]["containers"]["sut"]
    assert sut == {"image": "s", "resources": {"cpu": 8, "memory": "4Gi"}}
    assert cfg["execution"]["containers"]["scenario"] == {"image": "a"}


def test_the_extends_key_is_consumed(tmp_path):
    assert "extends" not in _load(tmp_path, "version: 4\nextends: base.vast\n", base=CAMPAIGN)


def test_a_chain_resolves_bases_first(tmp_path):
    """Each link overrides the one under it, so the nearest statement of a value wins."""
    _write(tmp_path, "a.vast",
           "execution:\n  containers: {scenario: {image: a}}\n  runs: 1\n"
           "  timeout: 10\n  shm_size: 1Gi\n")
    _write(tmp_path, "b.vast",
           "extends: a.vast\nexecution:\n  timeout: 20\n  shm_size: 2Gi\n")
    execution = _load(tmp_path, "version: 4\nextends: b.vast\n"
                                "execution:\n  shm_size: 3Gi\n")["execution"]
    assert (execution["runs"], execution["timeout"], execution["shm_size"]) == (1, 20, "3Gi")


def test_the_chain_is_archived_nearest_last(tmp_path):
    _write(tmp_path, "a.vast", BODY)
    _write(tmp_path, "b.vast", "extends: a.vast\n")
    child = _write(tmp_path, "campaign.vast", "version: 4\nextends: b.vast\n")
    assert [p.name for p in extends_sources({"version": 4, "extends": "b.vast"}, str(child))] \
        == ["a.vast", "b.vast", "campaign.vast"]


# -- lists replace, they never append ----------------------------------------------

@pytest.mark.parametrize("block, path, gone", [
    ("visualization:\n  results:\n    run_view:\n      panels: [{type: log}]\n",
     ("visualization", "results", "run_view", "panels"), "camera"),
    ("results_processing:\n  postprocessing: [rosbags_nav2bt_to_csv]\n",
     ("results_processing", "postprocessing"), "rosbags_to_csv"),
])
def test_a_child_list_replaces_the_base_list_rather_than_appending(tmp_path, block, path, gone):
    """The rule people meet first: a list is inherited whole or restated whole."""
    base = BODY + block.replace("log", "camera").replace("rosbags_nav2bt_to_csv",
                                                         "rosbags_to_csv")
    cfg = _load(tmp_path, "version: 4\nextends: base.vast\n" + block, base=base)
    node = cfg
    for key in path:
        node = node[key]
    assert len(node) == 1 and gone not in str(node)


def test_a_child_configuration_list_replaces_the_bases_entirely(tmp_path):
    cfg = _load(tmp_path, "version: 4\nextends: base.vast\nconfiguration:\n- name: mine\n",
                base=BODY + "configuration:\n- name: theirs\n- name: also-theirs\n")
    assert [c["name"] for c in cfg["configuration"]] == ["mine"]


# -- where a base may live ---------------------------------------------------------

def test_a_base_beside_the_campaign_is_fine(tmp_path):
    """There is no rule that a base must sit in a subdirectory."""
    assert _load(tmp_path, "version: 4\nextends: base.vast\n",
                 base=CAMPAIGN)["execution"]["runs"] == 1


def test_a_base_in_a_subdirectory_is_fine(tmp_path):
    _write(tmp_path, "common/base.vast", CAMPAIGN)
    assert _load(tmp_path, "version: 4\nextends: common/base.vast\n")["execution"]["runs"] == 1


def test_a_base_outside_the_project_directory_is_refused(tmp_path):
    """Only the project directory reaches a workspace, so an escaping base is simply absent
    wherever the campaign is not run from this tree."""
    _write(tmp_path, "outside/base.vast", CAMPAIGN)
    with pytest.raises(ValueError, match="outside the campaign's project directory"):
        _load(tmp_path / "proj", "version: 4\nextends: ../outside/base.vast\n")


def test_a_missing_base_is_refused_naming_it(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        _load(tmp_path, "version: 4\nextends: nope.vast\n")


def test_a_non_string_extends_is_refused(tmp_path):
    with pytest.raises(ValueError, match="must be a path"):
        _load(tmp_path, "version: 4\nextends: [a.vast, b.vast]\n")


# -- refusals that protect the reading ---------------------------------------------

def test_a_cycle_is_refused_and_the_error_shows_the_chain(tmp_path):
    _write(tmp_path, "a.vast", "extends: b.vast\n")
    _write(tmp_path, "b.vast", "extends: a.vast\n")
    with pytest.raises(ValueError, match="cycle detected") as exc:
        _load(tmp_path, "version: 4\nextends: a.vast\n")
    assert "->" in str(exc.value)


def test_a_self_extending_file_is_a_cycle(tmp_path):
    with pytest.raises(ValueError, match="cycle detected"):
        _load(tmp_path, "version: 4\nextends: campaign.vast\n")


def test_a_version_disagreement_is_refused_naming_both(tmp_path):
    """Inheriting a version silently would validate against one schema what was authored
    against another, with nothing saying so."""
    with pytest.raises(ValueError, match="must agree") as exc:
        _load(tmp_path, "version: 4\nextends: base.vast\n", base="version: 2\n" + BODY)
    assert "base.vast" in str(exc.value)


def test_a_base_without_a_version_inherits_the_childs(tmp_path):
    cfg = _load(tmp_path, "version: 4\nextends: base.vast\n",
                base="execution:\n  containers: {scenario: {image: a}}\n  runs: 4\n")
    assert cfg["version"] == 4 and cfg["execution"]["runs"] == 4


def test_a_partial_base_is_never_validated_on_its_own(tmp_path):
    """A base has no ``execution:`` and could not validate alone; only the merge is a document."""
    base = _write(tmp_path, "base.vast", "results_processing:\n  postprocessing: [x]\n")
    with pytest.raises(ValueError):
        load_config(str(base))
    cfg = _load(tmp_path, "version: 4\nextends: base.vast\n" + BODY)
    assert cfg["results_processing"]["postprocessing"] == ["x"]


# -- the archive -------------------------------------------------------------------

def _archive(tmp_path, project_vast):
    from robovast.common.execution import _archive_vast_sources
    config_dir = tmp_path / "campaign-dir" / "_config"
    config_dir.mkdir(parents=True)
    _archive_vast_sources(str(project_vast), str(config_dir))
    return config_dir


def test_a_campaign_extending_nothing_is_archived_byte_for_byte(tmp_path):
    """Comments and anchors survive because the file is copied, not re-serialised."""
    text = "version: 4\n# a comment nobody should lose\n" + BODY
    config_dir = _archive(tmp_path, _write(tmp_path / "proj", "campaign.vast", text))
    assert (config_dir / "campaign.vast").read_text(encoding="utf-8") == text


def test_the_chain_is_archived_at_the_paths_the_author_gave_it(tmp_path):
    """``reconstruct_project`` hands back a tree whose ``extends:`` still resolve."""
    proj = tmp_path / "proj"
    _write(proj, "base.vast", CAMPAIGN)
    _write(proj, "common/mid.vast", "extends: ../base.vast\n")
    config_dir = _archive(tmp_path, _write(proj, "campaign.vast",
                                           "version: 4\nextends: common/mid.vast\n"))
    assert sorted(p.relative_to(config_dir).as_posix()
                  for p in config_dir.rglob("*") if p.is_file()) == \
        [".campaign", "base.vast", "campaign.vast", "common/mid.vast"]


def test_the_pointer_beats_alphabetical_order(tmp_path):
    """The regression that matters: ``base.vast`` sorts first, and is not the campaign."""
    from robovast.common.results_utils import campaign_vast
    proj = tmp_path / "proj"
    _write(proj, "base.vast", CAMPAIGN)
    config_dir = _archive(tmp_path, _write(proj, "campaign.vast",
                                           "version: 4\nextends: base.vast\n"))
    assert sorted(config_dir.glob("*.vast"))[0].name == "base.vast"
    assert campaign_vast(config_dir.parent).name == "campaign.vast"


def test_a_campaign_archived_before_pointers_still_resolves(tmp_path):
    """One file, no pointer: the old rule and the new one agree, so nothing needs migrating."""
    from robovast.common.results_utils import campaign_vast
    config_dir = tmp_path / "legacy" / "_config"
    config_dir.mkdir(parents=True)
    (config_dir / "campaign.vast").write_text(CAMPAIGN, encoding="utf-8")
    assert campaign_vast(config_dir.parent).name == "campaign.vast"


def test_a_pointer_naming_a_missing_file_says_so(tmp_path):
    from robovast.common.results_utils import campaign_vast
    config_dir = tmp_path / "broken" / "_config"
    config_dir.mkdir(parents=True)
    (config_dir / "campaign.vast").write_text(CAMPAIGN, encoding="utf-8")
    (config_dir / ".campaign").write_text("gone.vast\n", encoding="utf-8")
    with pytest.raises(ValueError, match="gone.vast"):
        campaign_vast(config_dir.parent)


# -- which .vast in a workspace is the campaign ------------------------------------

def test_a_base_does_not_make_a_workspace_ambiguous(tmp_path):
    from robovast.service.local_transport import _extended_bases
    proj = tmp_path / "proj"
    base = _write(proj, "base.vast", CAMPAIGN)
    child = _write(proj, "campaign.vast", "version: 4\nextends: base.vast\n")
    assert _extended_bases([base, child], proj) == {base.resolve()}


def test_a_vast_nothing_extends_is_still_a_candidate(tmp_path):
    """An orphan is indistinguishable from a second campaign, and saying so is right."""
    from robovast.service.local_transport import _extended_bases
    proj = tmp_path / "proj"
    assert _extended_bases([_write(proj, "one.vast", CAMPAIGN),
                            _write(proj, "two.vast", CAMPAIGN)], proj) == set()


def test_a_half_written_campaign_is_not_mistaken_for_a_base(tmp_path):
    """It is missing ``execution:``, but nothing extends it, so it still resolves and the
    author gets a validation error naming the section rather than 'no .vast file'."""
    from robovast.service.local_transport import _extended_bases
    proj = tmp_path / "proj"
    draft = _write(proj, "draft.vast", "version: 4\nconfiguration:\n- name: a\n")
    assert _extended_bases([draft], proj) == set()
