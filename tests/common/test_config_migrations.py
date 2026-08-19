"""The config migration ladder: its invariants, its goldens, and its purity rule.

Deliberately does NOT reach into the parent superproject for the real version-1 campaigns
that motivated this ladder. ``robovast`` has to stand alone, so the shapes those files use
are reproduced in ``migrations/fixtures/v1.vast`` instead.
"""

import ast
import pathlib

import pytest
import yaml

from robovast.common.config import validate_config
from robovast.common.migrations import (BASELINE_CONFIG_VERSION, SUPPORTED_CONFIG_VERSION,
                                        ConfigTooNew, ConfigTooOld, ConfigVersionError,
                                        needs_upgrade, upgrade_config, upgrade_config_file)
from robovast.common.migrations import config as ladder

_MIGRATIONS_DIR = pathlib.Path(ladder.__file__).parent
_FIXTURES = _MIGRATIONS_DIR.parent / "fixtures"


def _load(path):
    """Parse a fixture, telling the reader what to do when it is still a placeholder.

    ``new_config_migration.py`` seeds a comments-only golden on purpose, so a scaffolded
    step fails until its transform is implemented. Parsing that yields no documents at all,
    and the bare IndexError said nothing about how to proceed.
    """
    path = pathlib.Path(path)
    documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    if not documents or documents[0] is None:
        raise AssertionError(
            f"{path.name} has no content yet -- it is the placeholder golden a scaffolded "
            f"step starts with. Implement the step, then:\n"
            f"  python3 tools/new_config_migration.py --regenerate <from-version>")
    return documents[0]


def _step_versions():
    """``[(from, to)]`` for every step in the ladder."""
    return [(v, v + 1) for v in range(BASELINE_CONFIG_VERSION, SUPPORTED_CONFIG_VERSION)]


def test_one_step_per_version_increment():
    """The constant and the ladder cannot disagree.

    Already asserted at import time in the ladder module -- restated here so the failure
    reads as a test rather than a collection error, which is what a contributor who bumped
    the constant and forgot the step will actually see.
    """
    assert len(ladder._MIGRATIONS) == SUPPORTED_CONFIG_VERSION - BASELINE_CONFIG_VERSION


@pytest.mark.parametrize("step_file", sorted(_MIGRATIONS_DIR.glob("v*_to_v*.py")))
def test_migration_purity(step_file):
    """A step must never import the current config model.

    This is the rule that keeps *old* steps correct. A step that consults the current
    pydantic model changes meaning every time that model changes, so a migration written
    today would silently produce something different a year from now -- and the campaigns
    it was written for are exactly the ones nobody re-tests. A comment cannot enforce that,
    so the import graph is checked instead.
    """
    tree = ast.parse(step_file.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = sorted(name for name in imported
                       if name == "robovast.common.config"
                       or name.startswith("robovast.common.config."))
    assert not forbidden, (
        f"{step_file.name} imports {forbidden}. A migration step must be a pure "
        f"dict -> dict transform -- see migrations/README.md.")


@pytest.mark.parametrize("from_version,to_version", _step_versions())
def test_step_golden(from_version, to_version):
    """Each step, applied alone, still produces exactly what it produced when written.

    Goldens are per-step rather than per-fixture-to-current on purpose: a to-current golden
    would be invalidated by every future version bump and so would pin nothing. Per-step,
    adding a step touches no existing golden -- and editing an existing step breaks its own.
    """
    source = (_FIXTURES / f"v{from_version}.vast" if from_version == BASELINE_CONFIG_VERSION
              else _FIXTURES / f"v{from_version - 1}_to_v{from_version}.expected.vast")
    golden = _FIXTURES / f"v{from_version}_to_v{to_version}.expected.vast"
    assert source.exists(), f"missing ladder input {source.name}"
    assert golden.exists(), (
        f"missing {golden.name}. Every step needs a golden; 'make new-config-migration' "
        f"creates one.")
    step = ladder._MIGRATIONS[from_version - BASELINE_CONFIG_VERSION]
    assert step(_load(source)) == _load(golden)


def test_baseline_reaches_supported_and_validates():
    """The whole chain, end to end: the oldest config becomes a valid current one."""
    upgraded, applied = upgrade_config(_load(_FIXTURES / f"v{BASELINE_CONFIG_VERSION}.vast"))
    assert upgraded["version"] == SUPPORTED_CONFIG_VERSION
    assert applied == [f"{a}_to_{b}" for a, b in _step_versions()]
    validate_config(upgraded)


def test_upgrade_does_not_mutate_its_input():
    """Callers hand us a config they still hold -- reading must not rewrite it."""
    raw = _load(_FIXTURES / f"v{BASELINE_CONFIG_VERSION}.vast")
    before = yaml.safe_dump(raw, sort_keys=True)
    upgrade_config(raw)
    assert yaml.safe_dump(raw, sort_keys=True) == before


def test_current_version_is_a_no_op():
    raw = {"version": SUPPORTED_CONFIG_VERSION, "execution": {}}
    upgraded, applied = upgrade_config(raw)
    assert applied == []
    assert upgraded is raw
    assert not needs_upgrade(raw)


def test_newer_version_is_refused_not_migrated():
    """A newer format cannot be migrated backwards, and pretending otherwise is worse."""
    with pytest.raises(ConfigTooNew) as excinfo:
        upgrade_config({"version": SUPPORTED_CONFIG_VERSION + 1})
    assert "upgrade robovast" in str(excinfo.value)


def test_version_below_baseline_is_refused():
    with pytest.raises(ConfigTooOld):
        upgrade_config({"version": BASELINE_CONFIG_VERSION - 1})


@pytest.mark.parametrize("raw", [{}, {"version": None}, {"version": "2"}])
def test_unusable_version_is_refused(raw):
    with pytest.raises(ConfigVersionError):
        upgrade_config(raw)


def test_secondary_containers_both_authored_shapes():
    """The name-maps-to-null shape must not lose a container's resources.

    Real v1 campaigns wrote ``- nav:`` with ``resources:`` as a *sibling*, which parses as
    ``{"nav": None, "resources": {...}}``. Reading only the documented nested shape would
    silently drop those resources, so both are covered here rather than only the one the
    documentation showed.
    """
    upgraded, _ = upgrade_config({
        "version": 1,
        "execution": {
            "image": "img:1",
            "secondary_containers": [
                {"nav": None, "resources": {"cpu": 2}},
                {"sim": {"resources": {"cpu": 3}}},
            ],
        },
    })
    containers = upgraded["execution"]["containers"]
    assert containers["nav"]["resources"] == {"cpu": 2}
    assert containers["sim"]["resources"] == {"cpu": 3}
    # v1 had one image per campaign and v2 has no inheritance: leaving these unset would
    # either fail validation (ad-hoc container) or silently adopt a different default.
    assert containers["nav"]["image"] == "img:1"
    assert containers["sim"]["image"] == "img:1"


def test_upgrade_config_file_preserves_comments(tmp_path):
    """A file a human will edit must keep its comments.

    The manual-migration workflow hands the upgraded file to a person precisely when
    decisions are needed; stripping their notes at that moment defeats the purpose.
    """
    target = tmp_path / "campaign.vast"
    target.write_text((_FIXTURES / f"v{BASELINE_CONFIG_VERSION}.vast").read_text(encoding="utf-8"),
                      encoding="utf-8")
    _, applied = upgrade_config_file(target, write=True)
    assert applied
    text = target.read_text(encoding="utf-8")
    assert f"version: {SUPPORTED_CONFIG_VERSION}" in text
    assert "# Baseline fixture for the config migration ladder" in text
    assert "name: ladder_fixture" in text


def test_upgrade_config_file_without_write_leaves_the_file_alone(tmp_path):
    target = tmp_path / "campaign.vast"
    original = (_FIXTURES / f"v{BASELINE_CONFIG_VERSION}.vast").read_text(encoding="utf-8")
    target.write_text(original, encoding="utf-8")
    upgraded, applied = upgrade_config_file(target)
    assert applied and upgraded["version"] == SUPPORTED_CONFIG_VERSION
    assert target.read_text(encoding="utf-8") == original
