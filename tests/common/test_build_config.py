# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``build:`` section: schema validation + build:<tag> image-ref consistency."""

import pytest

from robovast.common.config import BUILD_IMAGE_PREFIX, validate_config
from robovast.common.execution import (build_image_tag, is_build_image_ref,
                                        resolve_robovast_image)


def _cfg(**over):
    base = {"version": 1, "execution": {"image": "build:sim-suite", "runs": 1},
            "build": {"tag": "sim-suite",
                      "system_packages": ["ros-jazzy-nav2-smac-planner"],
                      "python_packages": ["packages/sim_suite_mobile", "shapely>=2.0"]}}
    base.update(over)
    return base


def test_build_section_validates():
    c = validate_config(_cfg())
    assert c.build.tag == "sim-suite"
    assert c.execution.image == "build:sim-suite"


def test_no_build_section_is_unchanged():
    c = validate_config({"version": 1,
                         "execution": {"image": "ghcr.io/x/y:1", "runs": 1}})
    assert c.build is None
    assert c.execution.image == "ghcr.io/x/y:1"


def test_build_ref_requires_matching_section():
    with pytest.raises(ValueError, match="no 'build:' section"):
        validate_config({"version": 1,
                         "execution": {"image": "build:foo", "runs": 1}})


def test_build_ref_tag_must_match():
    with pytest.raises(ValueError, match="does not match build.tag"):
        validate_config(_cfg(execution={"image": "build:other", "runs": 1}))


def test_build_section_needs_ref():
    with pytest.raises(ValueError, match="does not reference it"):
        validate_config(_cfg(execution={"image": "plain:1", "runs": 1}))


def test_tag_rejects_registry_host():
    with pytest.raises(ValueError, match="bare image name"):
        validate_config({"version": 1,
                         "execution": {"image": "build:ghcr.io/x/y", "runs": 1},
                         "build": {"tag": "ghcr.io/x/y"}})


def test_image_ref_helpers():
    assert is_build_image_ref("build:foo") is True
    assert is_build_image_ref("ghcr.io/x:1") is False
    assert build_image_tag("build:sim-suite") == "sim-suite"


def test_resolve_guards_unresolved_build_ref():
    # A build:<tag> that reaches image resolution (never built) fails loudly.
    with pytest.raises(ValueError, match="unresolved build image ref"):
        resolve_robovast_image(config_image=f"{BUILD_IMAGE_PREFIX}foo")
    # An explicit concrete image still wins and resolves fine.
    assert resolve_robovast_image(explicit="reg/x:1",
                                  config_image="build:foo") == "reg/x:1"


def test_run_image_required_fails_loud(monkeypatch):
    # The image a campaign RUNS must be pinned: nothing configured -> raise, not
    # silently use the mutable default tag.
    monkeypatch.delenv("ROBOVAST_IMAGE", raising=False)
    with pytest.raises(ValueError, match="no container image configured for this run"):
        resolve_robovast_image(required=True)
    # Explicit / config still satisfy the requirement.
    assert resolve_robovast_image(required=True, explicit="reg/x:1") == "reg/x:1"
    assert resolve_robovast_image(required=True, config_image="reg/y:2") == "reg/y:2"


def test_build_base_image_keeps_default(monkeypatch):
    # The build BASE image is not required: it defaults to the framework's own
    # published image (the normal base for experiment images).
    from robovast.common.execution import DEFAULT_ROBOVAST_IMAGE
    monkeypatch.delenv("ROBOVAST_IMAGE", raising=False)
    assert resolve_robovast_image() == DEFAULT_ROBOVAST_IMAGE
